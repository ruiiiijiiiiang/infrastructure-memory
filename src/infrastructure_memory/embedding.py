"""Pinned ONNX MiniLM inference, without PyTorch or runtime model downloads."""

import hashlib
import json

import numpy as np

MODEL_FILES = ["tokenizer.json", "sentence_bert_config.json", "onnx/model.onnx"]


class Embedder:
    def __init__(self, config):
        self.config = config
        self.fingerprint = hashlib.sha256(
            f"{config.model}@{config.revision}:mean-pool-v1".encode()
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
        path = snapshot_download(
            self.config.model,
            revision=self.config.revision,
            cache_dir=str(self.config.cache / "models"),
            local_files_only=True,
            allow_patterns=MODEL_FILES,
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        from pathlib import Path

        self._tokenizer = Tokenizer.from_file(str(Path(path) / "tokenizer.json"))
        model_config = json.loads(
            (Path(path) / "sentence_bert_config.json").read_text()
        )
        self.max_length = min(model_config["max_seq_length"], 256)
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._session = ort.InferenceSession(
            str(Path(path) / "onnx/model.onnx"),
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
            budget = self.max_length - 2
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
