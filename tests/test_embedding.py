import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

from infrastructure_memory.config import Config
from infrastructure_memory.embedding import MODEL_FILES, Embedder, fetch_model
from infrastructure_memory.events import new_event
from infrastructure_memory.search import Search
from infrastructure_memory.store import Store, digest


class EmbeddingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(
            self.root / "records",
            self.root / "state",
            self.root / "cache",
            self.root / "service.sock",
        )
        self.model = self.root / "model"
        (self.model / "1_Pooling").mkdir(parents=True)
        vocab = {
            "[UNK]": 0,
            "[CLS]": 1,
            "[SEP]": 2,
            "alpha": 3,
            "beta": 4,
            "gamma": 5,
            "delta": 6,
            "[PAD]": 7,
        }
        tokenizer = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = Whitespace()
        tokenizer.post_processor = TemplateProcessing(
            single="[CLS] $A [SEP]",
            special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
        )
        tokenizer.save(str(self.model / "tokenizer.json"))
        self.write("config.json", {"pad_token_id": 7})
        self.write("tokenizer_config.json", {"pad_token": "[PAD]"})
        self.write("sentence_bert_config.json", {"max_seq_length": 6})
        self.write("1_Pooling/config.json", {"pooling_mode_cls_token": True})
        self.feeds = []
        self.session = SimpleNamespace(
            get_inputs=lambda: [
                SimpleNamespace(name="input_ids"),
                SimpleNamespace(name="attention_mask"),
            ],
            run=self.infer,
        )
        self.download = self.enterContext(
            patch("huggingface_hub.snapshot_download", return_value=str(self.model))
        )
        self.session_factory = self.enterContext(
            patch("onnxruntime.InferenceSession", return_value=self.session)
        )

    def write(self, name, value):
        (self.model / name).write_text(json.dumps(value))

    def infer(self, outputs, feed):
        self.feeds.append(feed)
        ids = feed["input_ids"].astype(np.float32)
        # CLS and other token states differ; padding has a nonzero hidden state.
        return [np.stack([ids, np.ones_like(ids), np.zeros_like(ids)], axis=-1)]

    def test_default_loads_pinned_granite_fp32_offline_with_cls_pooling(self):
        embedder = Embedder(self.config)
        self.assertEqual(
            "ibm-granite/granite-embedding-97m-multilingual-r2", self.config.model
        )
        self.assertEqual(
            "835ad14087e140460703cf0fae09f97d469d65c2", self.config.revision
        )
        actual = embedder.encode(["alpha beta", "gamma"])
        expected = np.asarray([[1, 1, 0], [1, 1, 0]], dtype=np.float32) / np.sqrt(2)
        np.testing.assert_allclose(actual, expected)
        self.assertEqual(np.float32, actual.dtype)
        self.download.assert_called_once_with(
            self.config.model,
            revision=self.config.revision,
            cache_dir=str(self.config.cache / "models"),
            local_files_only=True,
            allow_patterns=MODEL_FILES,
        )
        args, kwargs = self.session_factory.call_args
        self.assertEqual(str(self.model / "onnx/model.onnx"), args[0])
        self.assertEqual(["CPUExecutionProvider"], kwargs["providers"])
        self.assertEqual(2, args[1].intra_op_num_threads)
        self.assertEqual(1, args[1].inter_op_num_threads)
        self.assertEqual({"input_ids", "attention_mask"}, self.feeds[0].keys())
        self.assertEqual(1, self.session_factory.call_count)

    def test_mean_pooling_ignores_padding_and_averages_long_input_segments(self):
        self.write("1_Pooling/config.json", {"pooling_mode_mean_tokens": True})
        embedder = Embedder(self.config)
        actual = embedder.encode(["alpha beta gamma delta alpha"])[0]
        expected = np.asarray([2.75, 1, 0], dtype=np.float32)
        expected /= np.linalg.norm(expected)
        np.testing.assert_allclose(actual, expected)
        np.testing.assert_array_equal(
            self.feeds[0]["input_ids"], [[1, 3, 4, 5, 6, 2], [1, 3, 2, 7, 7, 7]]
        )
        np.testing.assert_array_equal(
            self.feeds[0]["attention_mask"], [[1, 1, 1, 1, 1, 1], [1, 1, 1, 0, 0, 0]]
        )

    def test_token_budget_uses_actual_special_tokens_and_preserves_unicode_tail(self):
        tokenizer = Tokenizer.from_file(str(self.model / "tokenizer.json"))
        tokenizer.post_processor = TemplateProcessing(
            single="[CLS] $A [SEP] [SEP]",
            special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
        )
        tokenizer.save(str(self.model / "tokenizer.json"))
        Embedder(self.config).encode(["alpha β gamma delta"])
        np.testing.assert_array_equal(
            self.feeds[0]["input_ids"], [[1, 3, 0, 5, 2, 2], [1, 6, 2, 2, 7, 7]]
        )

    def test_native_context_is_capped_at_benchmarked_length(self):
        self.write("sentence_bert_config.json", {"max_seq_length": 32768})
        embedder = Embedder(self.config)
        embedder.encode(["alpha " * 300])
        self.assertEqual(256, embedder.max_length)
        self.assertEqual((2, 256), self.feeds[0]["input_ids"].shape)
        self.assertEqual(304, int(self.feeds[0]["attention_mask"].sum()))

    def test_empty_text_returns_a_finite_normalized_vector(self):
        vector = Embedder(self.config).encode([""])[0]
        self.assertTrue(np.isfinite(vector).all())
        self.assertAlmostEqual(1, float(np.linalg.norm(vector)), places=6)

    def test_invalid_model_metadata_fails_before_creating_session(self):
        for name, value, message in [
            ("1_Pooling/config.json", {"pooling_mode_lasttoken": True}, "pooling"),
            (
                "1_Pooling/config.json",
                {"pooling_mode_cls_token": True, "pooling_mode_mean_tokens": True},
                "pooling",
            ),
            ("config.json", {"pad_token_id": 0}, "padding"),
            ("sentence_bert_config.json", {"max_seq_length": 2}, "token limit"),
        ]:
            path = self.model / name
            original = path.read_text()
            with self.subTest(name=name, value=value):
                try:
                    self.write(name, value)
                    with self.assertRaisesRegex(ValueError, message):
                        Embedder(self.config).load()
                finally:
                    path.write_text(original)
        self.session_factory.assert_not_called()

    def test_fetch_downloads_fp32_and_metadata_for_later_offline_loading(self):
        self.assertEqual(str(self.model), fetch_model(self.config))
        self.download.assert_called_once_with(
            self.config.model,
            revision=self.config.revision,
            cache_dir=str(self.config.cache / "models"),
            allow_patterns=MODEL_FILES,
        )
        self.assertTrue(
            {
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "sentence_bert_config.json",
                "1_Pooling/config.json",
                "onnx/model.onnx",
            }.issubset(MODEL_FILES)
        )
        self.assertFalse(any("int8" in name for name in MODEL_FILES))

    def test_granite_and_rollback_do_not_reuse_legacy_minilm_vectors(self):
        store = Store(self.config)
        self.addCleanup(store.close)
        store.add(
            new_event(
                "proposal",
                {
                    "title": "DNS outage",
                    "text": "The router resolver stopped answering queries.",
                    "category": "incident",
                    "hosts": [],
                    "services": [],
                    "scope": "historical",
                    "observed_at": None,
                    "evidence": [],
                },
            )
        )
        doc = next(iter(store.documents().values()))
        hashed = digest((doc["title"] + "\n" + doc["text"]).encode())
        legacy = hashlib.sha256(
            b"sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41:mean-pool-v1"
        ).hexdigest()
        with store.db:
            store.db.execute(
                "INSERT INTO vectors VALUES (?, ?, ?)",
                (legacy, hashed, np.asarray([1, 0, 0], dtype=np.float32).tobytes()),
            )
        granite = Embedder(self.config)
        self.assertNotEqual(legacy, granite.fingerprint)
        search = Search(store, granite)
        search.refresh_vectors()
        self.assertIsNone(search.error)
        self.assertEqual(1, len(self.feeds))
        # A fresh Search reuses the new cache without rerunning inference.
        Search(store, Embedder(self.config)).refresh_vectors()
        self.assertEqual(1, len(self.feeds))
        self.config.model = "sentence-transformers/all-MiniLM-L6-v2"
        self.config.revision = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
        rollback = Embedder(self.config)
        self.assertNotEqual(legacy, rollback.fingerprint)
        self.assertNotEqual(granite.fingerprint, rollback.fingerprint)
        self.write("1_Pooling/config.json", {"pooling_mode_mean_tokens": True})
        search = Search(store, rollback)
        search.refresh_vectors()
        self.assertIsNone(search.error)
        self.assertEqual(2, len(self.feeds))
        self.assertEqual(
            3, store.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
