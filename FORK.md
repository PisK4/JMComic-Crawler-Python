# FORK 说明

本仓库是 [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
的 fork，供 **comic-intelligence** 项目（JM 本子归档）作为生产依赖使用。

- `origin` = `PisK4/JMComic-Crawler-Python`（本 fork）
- `upstream` = `hect0x7/JMComic-Crawler-Python`（上游）
- 基线版本：`v2.7.9`（2026-10-13）

## 为什么 fork 而不是依赖

上游是完整的协议实现（域名池、签名、AES、移动端 API），我们自研那部分等同于重造轮子。
但有三处需要改**内核**，用 monkeypatch 不干净：

1. **seed 边界语义**与站点实现不一致（见下）；
2. 上游**没有**处理「超高的页被纵向切成多片」这一现象（生态盲区）；
3. 需要一个能挂到下载流程上的固化插件。

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

## 同步上游

```bash
cd third_party/JMComic-Crawler-Python
git fetch upstream
git merge upstream/master        # 冲突只可能出现在上面 4 处
```

上游节奏约 1 周一次 release；近 6 个月 jm_plugin/jm_config/jm_toolkit 各被改动
7–10 次。改动的 4 处锚点行**近 3 个月未被动过**，冲突面很小。
