"""tests/test_encoding_detect.py — 编码择优回归测试（unittest，零依赖）

覆盖：
- 核心回归 #14 型：UTF-8 正文 + 聚集坏字节（0xE3/0x80）→ 必须判 utf-8（旧版误判 gb18030 conf=1.0）
- 反向用例：真 gb18030 带坏字节 → 仍判 gb18030（防过度修正成 utf-8）
- utf-8-sig BOM → utf-8-sig；纯 ASCII → utf-8；big5 繁体 → big5
- 文件级阶梯：8KB 英文元数据 + gb18030 正文 → 续读后判 gb18030（阶梯采样精髓不丢）

运行：python -m unittest discover tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from encoding_detect import detect_encoding, detect_encoding_bytes

# 简体中文正文（含全角标点，供 utf-8/gb18030 夹具）
_CN = "扶摇河山。作者：沧海不笑。混乱的时空，历史走进迷支……" * 20
# 繁体中文正文（big5 没有简体字形，须用繁体）
_TW = "扶搖河山。作者：滄海不笑。混亂的時空，歷史走進迷支……" * 20


def _bad_bytes(n: int = 150) -> bytes:
    """复现 #14 的聚集坏字节：0xE3 0x80 重复——非法 utf-8 序列，gb18030 却「假干净」。"""
    return b"\xe3\x80" * n


class TestDetectBytes(unittest.TestCase):
    def test_utf8_clean(self):
        enc, conf = detect_encoding_bytes(_CN.encode("utf-8"))
        self.assertEqual(enc, "utf-8")
        self.assertEqual(conf, 1.0)

    def test_utf8_sig_bom(self):
        enc, _ = detect_encoding_bytes(b"\xef\xbb\xbf" + _CN.encode("utf-8"))
        self.assertEqual(enc, "utf-8-sig")

    def test_ascii(self):
        enc, _ = detect_encoding_bytes(b"hello world, plain ascii.\n" * 10)
        self.assertEqual(enc, "utf-8")

    def test_utf8_with_corruption_regression_14(self):
        """核心回归：UTF-8 正文夹杂聚集坏字节（复现 #14），必须判 utf-8 而非 gb18030。"""
        body = _CN.encode("utf-8")
        raw = body[:1200] + _bad_bytes() + body[1200:]
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "utf-8")
        self.assertGreaterEqual(conf, 0.9)

    def test_gb18030(self):
        enc, _ = detect_encoding_bytes(_CN.encode("gb18030"))
        self.assertEqual(enc, "gb18030")

    def test_gb18030_with_corruption_not_utf8(self):
        """反向用例：真 gb18030 带坏字节也不得误判 utf-8（防过度修正）。"""
        raw = _CN.encode("gb18030") + _bad_bytes()
        enc, _ = detect_encoding_bytes(raw)
        self.assertEqual(enc, "gb18030")

    def test_big5(self):
        enc, _ = detect_encoding_bytes(_TW.encode("big5"))
        self.assertEqual(enc, "big5")


class TestDetectFile(unittest.TestCase):
    def _write(self, raw: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.write(raw)
        f.close()
        return Path(f.name)

    def test_english_head_then_gb18030_body(self):
        """阶梯采样精髓：8KB 内纯英文元数据不定案，续读后必须命中 gb18030 正文。"""
        head = b"Title: Some English Metadata. Author: Unknown.\n" * 900  # >8KB 纯英文
        p = self._write(head + _CN.encode("gb18030"))
        try:
            enc, _ = detect_encoding(p)
            self.assertEqual(enc, "gb18030")
        finally:
            p.unlink(missing_ok=True)

    def test_small_file_utf8(self):
        p = self._write(_CN.encode("utf-8"))
        try:
            enc, _ = detect_encoding(p)
            self.assertEqual(enc, "utf-8")
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
