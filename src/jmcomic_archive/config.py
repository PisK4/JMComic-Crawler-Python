#!/usr/bin/env python3
"""常量与配置 —— 全部上游参数集中于此。

对应文档：docs/02-upstream-protocol.md、docs/10-ops-guide.md §8
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = Path(os.environ.get('JMC_ARCHIVE', Path.home() / 'archives' / 'jmcomic'))
LIBRARY_DIR = ARCHIVE_ROOT / 'library'
META_DIR = LIBRARY_DIR / '_meta'
RANKING_DIR = META_DIR / 'rankings'
COMMENT_DIR = LIBRARY_DIR / '_comments'
DB_PATH = META_DIR / 'index.sqlite'

# ---------------------------------------------------------------- 域名
DOMAIN_SERVER_URLS = [
    'https://rup4a04-c01.tos-ap-southeast-1.bytepluses.com/newsvr-2025.txt',
    'https://rup4a04-c02.tos-cn-hongkong.bytepluses.com/newsvr-2025.txt',
    'https://rup4a04-c03.tos-cn-beijing.bytepluses.com.cn/newsvr-2025.txt',
]
DOMAIN_SERVER_SECRET = 'diosfjckwpqpdfjkvnqQjsik'

# 兜底域名（域名池不可用时）
FALLBACK_API_HOSTS = ['www.cdnhjk.net', 'www.cdngwc.cc', 'www.cdngwc.net', 'www.cdngwc.club']
FALLBACK_CDN_HOSTS = [
    'cdn-msp.jmapiproxy1.cc',
    'cdn-msp.jmapiproxy2.cc',
    'cdn-msp2.jmapiproxy2.cc',
    'cdn-msp3.jmapiproxy2.cc',
    'cdn-msp.jmapinodeudzn.net',
    'cdn-msp3.jmapinodeudzn.net',
]

# ---------------------------------------------------------------- 密钥
API_SECRET = '185Hcomic3PAPP7R'
SCRAMBLE_API_SECRET = '18comicAPPContent'   # 仅 /chapter_view_template
APP_VERSION = '2.1.11'

USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 9; V1938CT Build/PQ3A.190705.11211812; wv) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Safari/537.36'
)

# ---------------------------------------------------------------- 解扰
# 分段语义见 docs/04 §2.2：photo_id <= SCRAMBLE_220980 判不解扰；
# < SCRAMBLE_268850 固定切 10 片（不参与 md5）；< SCRAMBLE_421926 走 md5 % 10；否则 % 8。
SCRAMBLE_220980 = 220980    # scramble_id 实测当前值
SCRAMBLE_268850 = 268850    # 固定 10 片的上界（[220981, 268849] 曾误走 md5）
SCRAMBLE_421926 = 421926    # 2023-02-08 后的算法分界

# ---------------------------------------------------------------- 图片输出
# 归档落盘的是**解码后的成品图**（可直接浏览），原件保留在 _src/ 子目录。
# q92 实测比原件大 1.41×（lossless 则是 4.30×），见 docs/11 §5.7。
IMAGE_QUALITY = int(os.environ.get('JMC_IMAGE_QUALITY', '92'))

# ---------------------------------------------------------------- 上游切片
# 上游对高度超过此上限的页会**纵向切成多片**（满高片…满高片 + 余片），
# 判据见 docs/04 §5b：**高度恰为上限 = 本页未完**。
# ⚠️ 该值是**实测**所得（全库最大页高恰为 3600，且无一张 >3600）。
# 若上游调整上限，拼接规则会失效 —— 届时用 `tools/probe_slice_cap.py`
# 重新探测并改这里（或设环境变量 JMC_SLICE_CAP）。
SLICE_CAP_HEIGHT = int(os.environ.get('JMC_SLICE_CAP', '3600'))

# ---------------------------------------------------------------- 分页
PAGE_SIZE_LIST = 80         # /latest /search /categories/filter
PAGE_SIZE_PROMOTE = 27      # /promote_list
PAGE_SIZE_WEEK = 20         # /week/filter
PAGE_SIZE_COMMENT = 10      # /forum

# ---------------------------------------------------------------- 限速（保守）
API_RATE = 4.0              # req/s（实测上限 14，取 1/3 水位）
API_CONC = 4
API_BURST = 8

# 图片：CDN 由 CF 边缘直出（cf-cache-status HIT、Age≈78天，见 docs/04 §4），
# 实测 32 并发 320 请求零错误（docs/08 §7）。16 req/s 取实测上限 31.6 的 1/2。
IMG_RATE = float(os.environ.get('JMC_IMG_RATE', '16.0'))
IMG_CONC = int(os.environ.get('JMC_IMG_CONC', '12'))
IMG_BURST = 24

# 退避策略：**错误驱动**，不使用延迟反馈。
# 实测延迟反馈在并发下不可靠（两轮迭代都退化成棘轮，见 docs/11 §4.4）：
# 上游不返回 429，因此"无错误"就是最强的健康证据。
ERRORS_BEFORE_BACKOFF = 3       # 连续 N 次硬错误才减半
MIN_RATE = 1.0                  # 退避地板（req/s）

RETRY_TIMES = 3
RETRY_BACKOFF = 1.0         # 1 → 2 → 4 s
REQUEST_TIMEOUT = 30

# ---------------------------------------------------------------- 评论策略
# 用户明确要求：评论正文落盘（content_raw + content_text）
COMMENT_SAVE_BODY = True
COMMENT_MAX_PAGES = 60      # 单本评论页数硬上限（60 页 = 600 条）
COMMENT_PAGE_DELAY = 0.0    # 额外延迟，交给全局令牌桶

# ---------------------------------------------------------------- URL 模板
PHOTO_URL = 'https://{host}/media/photos/{pid}/{idx:05d}.webp'
COVER_URL = 'https://{host}/media/albums/{aid}_3x4.jpg'
USER_AVATAR_URL = 'https://{host}/media/users/{photo}'

# 可推导的**阅读链接**（上游 `real_link` 恒为空，故按路由自行拼）。
#
# 采用 jm.shixinn.com（JM Web PWA）。路由来自其前端 `js/app.js`：
#   /#/read/{章节id}   阅读器（mountReader(root, m[1], q)）
#   /#/album/{本子id}  本子页
# ⚠️ `#/read/` 取的是**章节 id（photo_id）**，不是本子 id。
#    单章本子两者相同；多章本子必须用具体章节 id，否则会打开错章。
READ_BASE = 'https://jm.shixinn.com/#'
READ_URL = READ_BASE + '/read/{cid}'
READ_ALBUM_URL = READ_BASE + '/album/{aid}'

# 上游官方站（保留备查）。实测被 Cloudflare 403 挡，仅供人工定位。
WEB_ALBUM_URL = 'https://18comic.vip/album/{aid}/'
WEB_PHOTO_URL = 'https://18comic.vip/photo/{cid}/'

SCHEMA_VERSION = 1
TOOL_VERSION = '0.1.0'


def ensure_dirs() -> None:
    for d in (LIBRARY_DIR, META_DIR, RANKING_DIR, COMMENT_DIR):
        d.mkdir(parents=True, exist_ok=True)
