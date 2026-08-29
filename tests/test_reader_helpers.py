from __future__ import annotations

import unittest

from school_assistant.wechat_reader import _decode, _xml_value


class ReaderHelperTests(unittest.TestCase):
    def test_xml_file_fields(self):
        xml = "<appmsg><title><![CDATA[课程通知.pdf]]></title><appattach><totallen>42</totallen></appattach></appmsg>"
        self.assertEqual(_xml_value(xml, "title"), "课程通知.pdf")
        self.assertEqual(_xml_value(xml, "totallen"), "42")

    def test_decode_utf8(self):
        self.assertEqual(_decode("通知".encode("utf-8")), "通知")


if __name__ == "__main__":
    unittest.main()
