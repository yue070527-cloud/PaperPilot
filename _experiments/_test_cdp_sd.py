"""
测试 CDP Fetcher 的 SD PDF popup + curl_cffi 方案。
"""
import logging
import sys
from paperpilot.cdp_fetcher import CDPFetcher, is_sciencedirect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("test")

# 测试论文：ScienceDirect 上的任意一篇
TEST_PAPER = {
    "title": "Test SD paper",
    "doi": "10.1016/j.artint.2023.103903",
    "url": "https://www.sciencedirect.com/science/article/pii/S0004370223001065",
}

def main():
    logger.info("=" * 60)
    logger.info("测试 1: is_sciencedirect 检测")
    assert is_sciencedirect(TEST_PAPER), "应该是 SD 论文"
    logger.info("  ✓ 正确识别为 SD 论文")

    logger.info("=" * 60)
    logger.info("测试 2: CDPFetcher 连接 + SD PDF 下载")

    with CDPFetcher(restart_edge_on_exit=True) as fetcher:
        logger.info("连接成功，开始下载 PDF...")
        pdf = fetcher.download_pdf(TEST_PAPER, timeout=90)

        if pdf:
            logger.info("✓ PDF 下载成功! %d bytes", len(pdf))
            # 验证 PDF 头
            assert pdf[:5] == b"%PDF-", "PDF 魔术数字错误"
            logger.info("✓ PDF 头验证通过")
            return 0
        else:
            logger.warning("✗ PDF 下载返回 None，尝试获取全文...")
            text = fetcher.fetch_full_text(TEST_PAPER, timeout=90)
            if text:
                logger.info("✓ 全文获取成功: %d chars", len(text))
                logger.info("前 300 字符:\n%s", text[:300])
            else:
                logger.error("✗ PDF 和全文均获取失败")
            return 1

if __name__ == "__main__":
    sys.exit(main())
