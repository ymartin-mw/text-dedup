#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Date    : 2022-11-05 11:03:18
# @Author  : Chenghao Mou (mouchenghao@gmail.com)
from __future__ import annotations

import argparse
import gc
import math
import multiprocessing as mp
import os
import pickle
import random
from collections import defaultdict
from itertools import permutations
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

import datasets
import numpy as np
import xxhash
from bitarray import bitarray
from bitarray import frozenbitarray
from datasets import load_dataset
from datasets import load_from_disk
from tqdm import tqdm

from text_dedup import logger
from text_dedup.utils import UnionFind
from text_dedup.utils import add_io_args
from text_dedup.utils import add_meta_args
from text_dedup.utils import add_simhash_args
from text_dedup.utils import ngrams
from text_dedup.utils.timer import Timer

datasets.logging.set_verbosity_error()


def _hamming_distance(a: bitarray, b: bitarray) -> int:
    """
    Compute the Hamming distance between two bitarrays.

    Parameters
    ----------
    a : bitarray
        The first bitarray.
    b : bitarray
        The second bitarray.

    Returns
    -------
    int
        The Hamming distance between the two bitarrays.

    Examples
    --------
    >>> _hamming_distance(bitarray('1010'), bitarray('1010'))
    0
    >>> _hamming_distance(bitarray('1010'), bitarray('0010'))
    1
    """
    return (a ^ b).count(1)


class Permutation:
    def __init__(self, f: int, k: int, b: int, masks: List[Tuple[bitarray, int, int, int]]) -> None:
        """
        A permutation object for bit manipulation.

        More details about this permutation can be found in https://github.com/seomoz/simhash-py#architecture.

        Parameters
        ----------
        f: int
            The fingerprint bit length
        k: int
            The bit difference allowed
        b: int
            The number of blocks
        masks:
            The block masks generated from `_create_permutations`
        """
        self.f = f
        self.k = k
        self.b = b

        width: int = 0
        self.widths: List[int] = []  # block widths
        self.offsets: List[int] = []  # block offsets
        self.reverse_masks: List[int] = []  # block reverse masks
        self.masks: List[bitarray] = []  # block masks
        for mask, mask_size, start, _ in masks:
            self.widths.append(mask_size)
            offset = start - width
            width += mask_size
            self.offsets.append(offset)
            if offset > 0:
                self.reverse_masks.append(mask << offset)
            else:
                self.reverse_masks.append(mask >> -offset)

            self.masks.append(mask)

        assert sum(self.widths) == f, "The sum of block widths must be equal to the fingerprint size"

        prefix_width = sum(self.widths[: b - k])
        self.search_mask: bitarray = bitarray(f)
        self.search_mask.setall(0)
        self.search_mask[:prefix_width] = 1
        self.search_mask = frozenbitarray(self.search_mask)

    def permute(self, x: bitarray) -> bitarray:
        """
        Permute the fingerprint.

        Parameters
        ----------
        x: bitarray
            The fingerprint to be permuted

        Returns
        -------
        bitarray
            The permuted fingerprint
        """
        result = bitarray(self.f)
        result.setall(0)

        for mask, offset in zip(self.masks, self.offsets):
            assert len(mask) == len(x) == self.f, f"{len(mask)}, {len(x)}, {self.f}"
            if offset > 0:
                result |= (x & mask) << offset
            else:
                result |= (x & mask) >> -offset

        return result


def _create_permutations(f: int, k: int, b: int) -> List[Permutation]:
    """
    Create permutations for f bits and b blocks with k-bit difference allowed.

    Parameters
    ----------
    f: int
        Fingerprint size
    k: int
        Bit difference allowed
    b: int
        Number of blocks

    Returns
    -------
    List[Permutation]
        The permutations

    Examples
    --------
    >>> from bitarray.util import urandom
    >>> perms = _create_permutations(128, 3, 4)
    >>> len(perms)
    4
    >>> data = urandom(128)
    >>> for perm in perms:
    ...     assert perm.reverse(perm.permute(data)) == data
    """
    block_size: int = math.ceil(f / b)
    masks: List[Tuple[bitarray, int, int, int]] = []

    for i in range(b):
        start, end = i * block_size, min((i + 1) * block_size, f)
        mask: bitarray = bitarray(f)
        mask.setall(0)
        mask[start:end] = 1
        masks.append(
            (
                mask,
                end - start,
                start,
                end,
            )
        )

    results: List[Permutation] = []
    # b - k many blocks must be the same
    indices = set(range(len(masks)))
    for leading_idx in permutations(range(len(masks)), b - k):
        remaining_idx = sorted(indices - set(leading_idx))
        blocks = [masks[i] for i in leading_idx] + [masks[i] for i in remaining_idx]
        results.append(Permutation(f, k, b, blocks))

    return results


def _unsigned_hash(obj: bytes, f: int = 64) -> bitarray:
    """
    Compute a hash of an object.

    It doesn't really matter what hash function to use, as long as it is consistent.

    Parameters
    ----------
    obj: bytes
        The object to hash.
    f: int
        The fingerprint size

    Returns
    -------
    bitarray
        The hash of the object.

    Examples
    --------
    >>> len(_unsigned_hash(b'hello world', f=64))
    64
    >>> len(_unsigned_hash(b'hello world', f=128))
    128
    """
    result = bitarray(0)
    match f:
        case 64:
            result.frombytes(xxhash.xxh64(obj).digest())
        case 128:
            result.frombytes(xxhash.xxh128(obj).digest())
        case _:
            raise ValueError(f"Unsupported fingerprint size: {f}")
    return result


def compute(hashes: List[bitarray]) -> bitarray:
    """
    Compute the Simhash of a list of hashes.

    Notes to myself: You tried porting this to Cython, but it didn't improve the performance.

    Parameters
    ----------
    hashes : List[int]
        The list of hashes.

    Returns
    -------
    bitarray
        The Simhash of the list of hashes.

    Examples
    --------
    >>> from bitarray.util import int2ba, ba2int
    >>> res = compute([int2ba(13352372148217134600, length=64), int2ba(5020219685658847592, length=64)])
    >>> ba2int(res)
    74633958390507528
    """
    sigs = np.asarray([h.tolist() for h in hashes], dtype=int)
    sig = np.where(np.sum(2 * sigs - 1, axis=0) > 0, 1, 0).astype(bool)
    res = bitarray()
    res.pack(sig.tobytes())
    return res


def embed_func(content: str, idx: int, *, f: int, ngram: int, permutations: List[Permutation] = None) -> Dict[str, Any]:
    """
    Calculate the simhash signature of a text.

    Parameters
    ----------
    content : str
        The text to be hashed.
    f : int
        The fingerprint size.
    idx : int
        The index of the text.
    ngram : int
        The ngram size.

    Returns
    -------
    Dict[str, Any]
        The simhash signature and the index of the text as a dictionary.

    Examples
    --------
    >>> res = embed_func("hello world", 0, f=64, ngram=3)
    >>> res["__id__"]
    0
    >>> len(res["__signature__"])
    8
    """
    tokens = {"".join(ng) for ng in ngrams(list(content), n=ngram)}
    sig = compute([_unsigned_hash(t.encode("utf-8"), f=f) for t in tokens])
    keys: List[Tuple[bytes, bytes]] = []
    if permutations:
        for permutation in permutations:
            keys.append(
                (
                    permutation.search_mask.tobytes(),
                    (permutation.permute(sig) & permutation.search_mask).tobytes(),
                )
            )
    return {"__signature__": sig.tobytes(), "__id__": idx, "__keys__": keys}


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        prog="text_dedup.simhash",
        description="Deduplicate text using simhash",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser = add_io_args(parser)
    parser = add_meta_args(parser)
    parser = add_simhash_args(parser)
    args = parser.parse_args()

    mp.set_start_method("fork", force=True)
    uf = UnionFind()
    timer = Timer()
    PERMUTATIONS = _create_permutations(args.f, k=args.bit_diff, b=args.num_bucket)
    BUCKETS: Dict[Any, List] = defaultdict(list)

    with timer("Total"):
        with timer("Loading"):
            if args.local:
                ds = load_from_disk(args.path)
            else:
                ds = load_dataset(
                    path=args.path,
                    name=args.name,
                    data_dir=args.data_dir,
                    data_files=args.data_files,
                    split=args.split,
                    revision=args.revision,
                    cache_dir=args.cache_dir,
                    use_auth_token=args.use_auth_token,
                )

        DATA_SIZE = len(ds)

        with timer("SimHashing"):
            embedded = ds.map(
                function=embed_func,
                fn_kwargs={"ngram": args.ngram, "permutations": PERMUTATIONS, "f": args.f},
                input_columns=[args.column],
                remove_columns=[args.column],
                num_proc=os.cpu_count(),
                with_indices=True,
                desc=f"SimHashing...",
            )

        # TODO Create multiple BUCKETS for parallelization
        with timer("Clustering"):
            for i in tqdm(
                range(0, len(embedded), args.batch_size),
                dynamic_ncols=True,
                desc="Iterating SimHashes...",
            ):
                batch = embedded[i : i + args.batch_size]
                for idx, keys, sig in tqdm(
                    zip(batch["__id__"], batch["__keys__"], batch["__signature__"]),
                    desc="Indexing...",
                    leave=False,
                    total=len(batch["__id__"]),
                ):
                    sig = frozenbitarray(buffer=sig)
                    neighbors = set()
                    for key in keys:
                        key = tuple(key)
                        for idy, other_fingerprint in BUCKETS[key]:
                            if idy in neighbors:
                                continue
                            if _hamming_distance(sig, other_fingerprint) <= args.bit_diff:
                                neighbors.add(idy)
                        BUCKETS[key].append((idx, sig))

                    for idy in neighbors:
                        uf.union(idx, idy)

        with timer("Filtering"):
            gc.freeze()
            gc.disable()
            ds = ds.map(
                function=lambda _, idx: {"__cluster__": uf.find(idx)},
                with_indices=True,
                num_proc=os.cpu_count(),
                new_fingerprint=str(random.getrandbits(128)),
                desc="Finding clusters...",
            )
            gc.enable()
            gc.collect()
            # This is where the deduplication happens
            # Since there is no easy groupby in datasets
            # I will use this simple filter for now
            final_data = ds.filter(
                function=lambda record, idx: record["__cluster__"] == idx,
                with_indices=True,
                num_proc=os.cpu_count(),
                desc="Filtering clusters...",
            )

        with timer("Saving"):
            final_data = final_data.remove_columns(["__cluster__"])
            final_data.save_to_disk(args.output)
            if args.debug:
                with open(os.path.join(args.output, "uf.pkl"), "wb") as f:
                    pickle.dump(uf, f, protocol=pickle.HIGHEST_PROTOCOL)

    PAD = 32
    for k, v in timer.elapsed_times.items():
        logger.info(f"{k:<{PAD}}: {v:.2f}s")

    logger.info(f"{'Before':<{PAD}}: {len(ds)}")
    logger.info(f"{'After':<{PAD}}: {len(final_data)}")
