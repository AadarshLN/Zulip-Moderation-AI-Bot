from __future__ import annotations

import torch.nn as nn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multioutput import MultiOutputClassifier
from transformers import AutoModel


class TransformerMultiHeadModel(nn.Module):
    def __init__(self, encoder_name: str, dropout: float = 0.1):
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden_size = self.encoder.config.hidden_size
        self.dropout = nn.Dropout(dropout)
        self.classifier = nn.Linear(hidden_size, 2)

    def forward(self, input_ids, attention_mask):
        outputs = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        cls = outputs.last_hidden_state[:, 0]
        cls = self.dropout(cls)
        logits = self.classifier(cls)  # [batch, 2]
        return logits


class TfidfLogRegMultiOutput:
    def __init__(
        self,
        max_features: int = 50000,
        ngram_range: tuple[int, int] = (1, 2),
        c: float = 1.0,
        max_iter: int = 1000,
    ):
        self.vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=ngram_range,
            lowercase=True,
            strip_accents="unicode",
        )
        base_lr = LogisticRegression(
            C=c,
            max_iter=max_iter,
            solver="liblinear",
            class_weight=None,
        )
        self.model = MultiOutputClassifier(base_lr)

    def fit(self, texts, y):
        x = self.vectorizer.fit_transform(texts)
        self.model.fit(x, y)

    def predict(self, texts):
        x = self.vectorizer.transform(texts)
        return self.model.predict(x)

    def predict_proba(self, texts):
        x = self.vectorizer.transform(texts)
        probs = self.model.predict_proba(x)
        suicide_prob = probs[0][:, 1]
        toxicity_prob = probs[1][:, 1]
        return suicide_prob, toxicity_prob
