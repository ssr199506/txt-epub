# -*- coding: utf-8 -*-
import unittest, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from encoding_detect import detect_encoding_bytes

# UTF-8 字节 fixtures (pure ASCII source via \x escapes)
UTF8_CJK = b"\xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0" + b"\xef\xbc\x8c" * 6   # "第一章" + 6 全角逗号
GB18030_CJK = b"\xd7\xa3\xd2\xbb\xd5\xc2" + b"\xa3\xac" * 6               # "第一章" gb18030 + 全角逗号
BIG5_CJK = b"\xa4\xa4\xa4\xe5" + b"\xa1\x43" * 6                          # "中文" big5 + 全角逗号

class TestEncodingDetect(unittest.TestCase):
    def test_utf8_with_bad_bytes(self):
        # #14 类：utf-8 正文 + 尾部坏首字节（聚集坏字节模拟）
        raw = UTF8_CJK + b"\xe4" * 200
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "utf-8")

    def test_gb18030_real_with_bad_bytes(self):
        # 反向用例：真 gb18030 + 坏字节，不能被过度修正成 utf-8
        raw = GB18030_CJK + b"\x00" * 50
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "gb18030")

    def test_utf8_sig_bom(self):
        raw = b"\xef\xbb\xbf" + UTF8_CJK
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "utf-8-sig")

    def test_plain_ascii(self):
        raw = b"Chapter 1 Hello World No Chinese Here."
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "utf-8")

    def test_big5(self):
        raw = BIG5_CJK
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "big5")

    def test_english_prefix_then_gb18030(self):
        # 英文序在前 + gb18030 正文：不能被 8KB 英文段误判 utf-8
        raw = (b"Preface English metadata, no chinese signal here. " * 30) + GB18030_CJK
        enc, conf = detect_encoding_bytes(raw)
        self.assertEqual(enc, "gb18030")

    def test_truncated_sample_no_crash(self):
        # 截断在多字节字符中间：必须不抛异常（修复 unexpected end of data 崩溃）
        raw = UTF8_CJK[:5]   # 截断"第一章"中间
        try:
            enc, conf = detect_encoding_bytes(raw)
        except Exception as e:
            self.fail("truncated sample raised %r" % e)
        self.assertIn(enc, ("utf-8", "utf-8-sig", "gb18030", "big5"))

    def test_empty(self):
        enc, conf = detect_encoding_bytes(b"")
        self.assertIn(enc, ("utf-8", "gb18030"))

if __name__ == "__main__":
    unittest.main()
