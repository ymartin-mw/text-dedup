#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Date    : 2022-04-02 11:26:04
# @Author  : Chenghao Mou (mouchenghao@gmail.com)

from typing import List

import numpy as np
import torch
from text_dedup.embedders import Embedder


class TransformerEmbedder(Embedder):
    def __init__(self, tokenizer, model):
        self.tokenizer = tokenizer
        self.model = model

    def embed(self, corpus: List[str], batch_size: int = 8) -> np.ndarray:

        embeddings = []
        for i in range(0, len(corpus), batch_size):
            batch = corpus[i : i + batch_size]
            encodings = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                return_tensors="pt",
            )

            with torch.no_grad():
                output = self.model(**encodings, output_hidden_states=True)
                hidden = output.hidden_states[-1]
                embeddings.extend(hidden.mean(dim=1).detach().cpu().numpy())

        return np.asarray(embeddings)
