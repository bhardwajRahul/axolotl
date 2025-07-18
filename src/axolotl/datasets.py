"""Module containing Dataset functionality"""

import torch
from datasets import Dataset, IterableDataset

from axolotl.utils.logging import get_logger

from .prompt_tokenizers import PromptTokenizingStrategy

# We want this to be a wrapper for an existing dataset that we have loaded
# lets use the concept of middlewares to wrap each dataset, for example
# ConstantLengthDataset(ShuffledDataset([TokenizedPromptDataset(alpaca_dataset)]))
# let's check to ensure we don't truncate an item in the middle, we'll use
# the collators later on to pad the datasets

LOG = get_logger(__name__)


class TokenizedPromptDataset(Dataset):
    """Dataset that returns tokenized prompts from a stream of text files.

    Args:
        prompt_tokenizer: The prompt tokenizing method for processing the data.
        dataset: Dataset with text files.
        process_count: Number of processes to use for tokenizing.
        keep_in_memory: Whether to keep the tokenized dataset in memory.
    """

    def __init__(  # pylint: disable=super-init-not-called
        self,
        prompt_tokenizer: PromptTokenizingStrategy,
        dataset: Dataset,
        process_count: int | None = None,
        keep_in_memory: bool | None = False,
        **kwargs,
    ):
        self.prompt_tokenizer = prompt_tokenizer
        self.process_count = process_count
        self.keep_in_memory = keep_in_memory
        super().__init__(
            self.process(dataset).data,
            **kwargs,
        )

    def process(self, dataset):
        features = dataset.features.keys()

        map_kwargs = {}
        if self.prompt_tokenizer.supports_batched:
            map_kwargs["batched"] = True
            map_kwargs["batch_size"] = 1_000

        if (
            hasattr(self.prompt_tokenizer, "filter_rows")
            and self.prompt_tokenizer.filter_rows
        ):
            dataset = dataset.filter(
                self.prompt_tokenizer.filter_rows,
                num_proc=self.process_count,
                desc="Strategy Filtering Rows",
            )

        return dataset.map(
            self.prompt_tokenizer.tokenize_prompt,
            num_proc=self.process_count,
            remove_columns=features,
            keep_in_memory=self.keep_in_memory,
            desc="Tokenizing Prompts",
            **map_kwargs,
        )


def wrap_dataset_for_tokenized_prompt(
    prompt_tokenizer: PromptTokenizingStrategy,
    dataset: Dataset | IterableDataset,
    **kwargs,
):
    if isinstance(dataset, IterableDataset):
        map_kwargs = {}
        if prompt_tokenizer.supports_batched:
            map_kwargs["batched"] = True
        features = list(dataset.features.keys())
        return dataset.map(
            prompt_tokenizer.tokenize_prompt,
            remove_columns=features,
            **map_kwargs,
        )
    return TokenizedPromptDataset(prompt_tokenizer, dataset, **kwargs)


# TODO this isn't the best since it can't interleave datasets
class ConstantLengthDataset(IterableDataset):
    """Iterable dataset that returns constant length chunks of tokens from stream of
    text files.

    Args:
        tokenizer: The processor used for processing the data.
        dataset: Dataset with text files.
        seq_length: Length of token sequences to return.
    """

    def __init__(  # pylint: disable=super-init-not-called
        self,
        tokenizer,
        datasets,
        seq_length=2048,
    ):
        self.tokenizer = tokenizer
        self.concat_token_id = tokenizer.eos_token_id
        self.datasets: list[IterableDataset] = datasets
        self.seq_length = seq_length

        vocab_size = len(tokenizer.get_vocab())

        if vocab_size <= torch.iinfo(torch.int16).max:
            self.tokens_dtype = torch.int16
        elif vocab_size <= torch.iinfo(torch.int32).max:
            self.tokens_dtype = torch.int32
        else:
            self.tokens_dtype = torch.int64

    def __iter__(self):
        buffer = {
            "input_ids": [],
            "attention_mask": [],
            "labels": [],
            "position_ids": [],
        }
        buffer_len = 0
        for dataset in self.datasets:
            idx = 0
            iterator = iter(dataset)
            more_examples = True
            while more_examples:
                try:
                    example = next(iterator)
                    idx += 1
                except StopIteration:
                    more_examples = False
                    example = None

                add_concat_token = False
                if example:
                    example_len = len(example["input_ids"])
                    add_concat_token = example["input_ids"][-1] != self.concat_token_id
                else:
                    example_len = 0

                if not example_len or (
                    buffer_len + int(add_concat_token) + example_len > self.seq_length
                ):
                    if buffer["input_ids"]:
                        input_ids = torch.cat(buffer["input_ids"], dim=-1)[
                            : self.seq_length
                        ]
                        attention_mask = torch.cat(buffer["attention_mask"], dim=-1)[
                            : self.seq_length
                        ]
                        position_ids = torch.cat(buffer["position_ids"], dim=-1)[
                            : self.seq_length
                        ]
                        labels = torch.cat(buffer["labels"], dim=-1)[: self.seq_length]
                        if labels.size() == input_ids.size() and (
                            attention_mask.size() == input_ids.size()
                        ):
                            yield {
                                "input_ids": input_ids,
                                "labels": labels,
                                "attention_mask": attention_mask,
                                "position_ids": position_ids,
                            }
                        else:
                            LOG.warning(
                                "Dropping batch due to tensor size mismatch "
                                f"input_ids: {input_ids.size()}, "
                                f"labels: {labels.size()}, "
                                f"attention_mask: {attention_mask.size()}"
                            )
                    buffer = {
                        "input_ids": [],
                        "attention_mask": [],
                        "labels": [],
                        "position_ids": [],
                    }
                    buffer_len = 0
                    idx = 1

                if example:
                    # FIXME
                    # just going to drop data points that are too long
                    if len(example["input_ids"]) <= self.seq_length:
                        input_ids = example["input_ids"]
                        attention_mask = example["attention_mask"]
                        labels = example["labels"]

                        if add_concat_token:
                            input_ids.append(self.concat_token_id)
                            attention_mask.append(1)
                            labels.append(self.concat_token_id)

                        input_ids_with_concat = torch.tensor(
                            input_ids, dtype=self.tokens_dtype
                        )
                        attention_mask_with_concat = torch.tensor(
                            [idx * m for m in attention_mask], dtype=torch.int16
                        )
                        labels_with_concat = torch.tensor(
                            labels, dtype=self.tokens_dtype
                        )
                        position_ids = torch.arange(
                            len(input_ids), dtype=self.tokens_dtype
                        )

                        buffer["input_ids"].append(input_ids_with_concat)
                        buffer["attention_mask"].append(attention_mask_with_concat)
                        buffer["labels"].append(labels_with_concat)
                        buffer["position_ids"].append(position_ids)
                        buffer_len += len(input_ids)
