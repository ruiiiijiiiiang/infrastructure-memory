"""Pinned ONNX CPU inference, without PyTorch or runtime model downloads."""

import hashlib
import json
from pathlib import Path

import numpy as np

MODEL_FILE = "onnx/model.onnx"
MAX_LENGTH = 256
PIPELINE_VERSION = "metadata-pool-segments-v2"
MODEL_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
    MODEL_FILE,
]


class Embedder:
    def __init__(self, config):
        self.config = config
        self.fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "model": config.model,
                    "revision": config.revision,
                    "file": MODEL_FILE,
                    "max_length": MAX_LENGTH,
                    "pipeline": PIPELINE_VERSION,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self._session = None
        self._tokenizer = None

    def load(self):
        if self._session is not None:
            return
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        ort.disable_telemetry_events()
        path = Path(
            snapshot_download(
                self.config.model,
                revision=self.config.revision,
                cache_dir=str(self.config.cache / "models"),
                local_files_only=True,
                allow_patterns=MODEL_FILES,
            )
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1

        self._tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        model_config = json.loads((path / "config.json").read_text())
        tokenizer_config = json.loads((path / "tokenizer_config.json").read_text())
        sentence_config = json.loads((path / "sentence_bert_config.json").read_text())
        pooling_config = json.loads((path / "1_Pooling/config.json").read_text())
        modes = {
            key
            for key, enabled in pooling_config.items()
            if key.startswith("pooling_mode_") and enabled
        }
        if modes == {"pooling_mode_cls_token"}:
            self._pooling = "cls"
        elif modes == {"pooling_mode_mean_tokens"}:
            self._pooling = "mean"
        else:
            raise ValueError("embedding model must use CLS or mean pooling exclusively")
        self.max_length = min(sentence_config["max_seq_length"], MAX_LENGTH)
        self._special_tokens = self._tokenizer.num_special_tokens_to_add(False)
        if self.max_length <= self._special_tokens:
            raise ValueError("embedding token limit leaves no room for text")
        pad_token = tokenizer_config["pad_token"]
        if isinstance(pad_token, dict):
            pad_token = pad_token["content"]
        pad_id = model_config["pad_token_id"]
        if pad_id is None or self._tokenizer.token_to_id(pad_token) != pad_id:
            raise ValueError(
                "embedding padding token does not match model configuration"
            )
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding(
            pad_id=pad_id, pad_token=pad_token, direction="right"
        )
        self._session = ort.InferenceSession(
            str(path / MODEL_FILE),
            options,
            providers=["CPUExecutionProvider"],
        )

    def encode(self, texts):
        self.load()
        result = []
        for text in texts:
            # Split by tokenizer offsets, preserving long identifiers and avoiding silent truncation.
            self._tokenizer.no_truncation()
            full = self._tokenizer.encode(text, add_special_tokens=False)
            segments = []
            budget = self.max_length - self._special_tokens
            for start in range(0, len(full.ids), budget):
                end = min(start + budget, len(full.ids))
                left, right = full.offsets[start][0], full.offsets[end - 1][1]
                segments.append(text[left:right])
            self._tokenizer.enable_truncation(max_length=self.max_length)
            encoded = self._tokenizer.encode_batch(segments or [text])
            feed = {
                "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
                "attention_mask": np.asarray(
                    [item.attention_mask for item in encoded], dtype=np.int64
                ),
                "token_type_ids": np.asarray(
                    [item.type_ids for item in encoded], dtype=np.int64
                ),
            }
            names = {item.name for item in self._session.get_inputs()}
            output = self._session.run(
                None, {k: v for k, v in feed.items() if k in names}
            )[0]
            if self._pooling == "cls":
                pooled = output[:, 0, :]
            else:
                mask = feed["attention_mask"][..., None]
                pooled = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            vector = pooled.mean(axis=0).astype(np.float32)
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            result.append(vector)
        return np.asarray(result, dtype=np.float32)


def fetch_model(config):
    from huggingface_hub import snapshot_download

    return snapshot_download(
        config.model,
        revision=config.revision,
        cache_dir=str(config.cache / "models"),
        allow_patterns=MODEL_FILES,
    )
