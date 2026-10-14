# FORK 说明

本仓库是 [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
的 fork，是 **comic-intelligence** 项目的**唯一主仓**：上游协议与本项目的归档层同仓。

- `origin` = `PisK4/JMComic-Crawler-Python`（本 fork）
- `upstream` = `hect0x7/JMComic-Crawler-Python`（上游）
- 基线版本：`v2.7.9`（2026-10-13）

## 仓库布局

```
src/jmcomic/           上游协议（域名池/签名/AES/下载器）—— 只做最小改动
src/jmcomic_archive/   本项目的归档层（限速/索引/编排/评论/CLI）
```

**约定**：上游文件只做最小改动（共 11 行 + 1 个新文件），本项目逻辑集中在
`jmcomic_archive/`，以压低每次 merge upstream 的冲突面。

## 为什么 fork 而不是依赖

上游是完整的协议实现（域名池、签名、AES、移动端 API），本项目原先自研那部分
等同于重造轮子。但有几处需要改**内核或下载流程**，用 monkeypatch 不干净：

1. **seed 边界语义**与站点实现不一致（见下）；
2. 上游**没有**处理「超高的页被纵向切成多片」这一现象（生态盲区）；
3. 需要一个能挂到下载流程上的固化插件；
4. 上游**完全没有限速**，而归档必须守住实测的请求水位。

## 与上游的差异（全部改动）

### 1. `src/jmcomic/jm_config.py` — 新增常量

```python
SLICE_CAP_HEIGHT = 3600   # 上游对高度超过此上限的页纵向切分
```

实测依据：全库最大页高恰为 `3600`，无一张 >3600（comic-intelligence `docs/04` §5b）。

### 2. `src/jmcomic/jm_toolkit.py` — 修正 seed 边界

```diff
- if aid < scramble_id:
+ if aid <= scramble_id:
      return 0
```

上游用严格小于（`aid == scramble_id` 会走「固定 10 片」）；站点 `descramble.js`
用 `photoId <= scrambleId → false`。本项目采纳站点语义。

> 该边界上游已无实测素材（`220980` 无对应页面），因此这是一处**有意的分歧**，
> 不是 bug 修复。若上游将来裁决该边界，需要重新评估。

### 3. `src/jmcomic/jm_archive_ext.py` — 新增（**全部自研逻辑集中于此**）

`SliceStitchPlugin`：把上游纵向切开的页拼回完整页，挂在 `after_photo` / `after_album`。

判定规则为**结构性**（高度恰为上限 = 本页未完），不做像素比较 —— 页面切点常落在
装订白槽，跨页比较会给出接近 0 的差异并导致**跨页误拼**（实测踩坑）。

### 4. `src/jmcomic/__init__.py` — 导入扩展模块

```diff
+ from .jm_archive_ext import *
```

一行，依赖上游已有的「自动注册 `JmOptionPlugin` 子类」机制。

### 5. `src/jmcomic_archive/` — 新增：归档层

本项目原先独立的 `collector/`（3,522 行）已并入此处，`collector/` **全数删除**。

| 模块 | 作用 |
|---|---|
| `config.py` | 路径、限速水位、上游模板常量 |
| `throttle.py` | 令牌桶 + 健康退避（**上游完全没有限速**） |
| `client.py` | `ArchiveClient`：限速注入、原件 `_src/` 留存、榜单全字段 |
| `errors.py` | 上游异常 → `ProtocolError` / `AlbumMissing` |
| `seed.py` | 解扰 seed（委托上游 `JmImageTool.get_num`） |
| `store.py` | SQLite 索引、原子落盘、sidecar |
| `decode.py` | 解扰、切片拼接、webp 编码 |
| `discover.py` | 榜单、详情、页清单、评论 |
| `archive.py` | 编排：本子 → 章节 → 页 → 评论 |
| `cli.py` | `jmc` 命令行 |

被删除的自研部分：协议客户端（与上游重复）与 279 行 HTTP 阅读器
（由上游 `jm_server` 插件覆盖）。

详见 comic-intelligence 的 `docs/12` §8。

### 6. `pyproject.toml` — 入口点与打包

```diff
+ jmc = "jmcomic_archive.cli:main"
+ [tool.setuptools.packages.find]
+ where = ["src"]
```

## 隔离约定（重要）

本 fork 的**包名与上游相同**（`jmcomic`），因此**不能与官方 `jmcomic` 装进同一个 venv**。
项目用独立虚拟环境隔离：

| venv | 装的东西 | 用途 |
|---|---|---|
| `.venv` | 官方 `jmcomic` | `tools/verify_*.py` 的 **ground truth** 比对 |
| `.venv-fork` | 本 fork（editable） | 采集生产依赖 |

若装进同一个 venv，`get_num` 的 GT 比对会变成**自证循环**
（用我们改过的实现去验证我们改过的实现）。

> 替代方案是重命名包为 `jmcomic_archive`。未采用：`jm_plugin.py` /
> `jm_config.py` / `jm_toolkit.py` 恰是上游改动最频繁的文件（近 6 个月 7–10 次），
> 重命名会让每次 merge 都撞路径。
>
> **改动只活在本仓库**：上游主仓（`workspace_intelligence`）把它作为 gitlink
> 引用，**不追踪这里的源码**。改完必须 `git push origin master`，否则定制会丢。

## 同步上游

```bash
cd third_party/JMComic-Crawler-Python
git fetch upstream
git merge upstream/master        # 冲突只可能出现在上面 4 处
```

上游节奏约 1 周一次 release；近 6 个月 jm_plugin/jm_config/jm_toolkit 各被改动
7–10 次。改动的 4 处锚点行**近 3 个月未被动过**，冲突面很小。
