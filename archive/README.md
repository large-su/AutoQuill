# 归档代码（已退役）

| 项目 | 内容 |
|---|---|
| **适用版本** | 历史记录（v2.x–v3.x 时期） |
| **最后更新** | 2026-10-01 |
| **状态** | 已归档 |
| **说明** | v4.1.0 精简体积时退役的 OCR / UI Automation / 图像生成链路的代码留存 |

## 保留原因

这些模块已不参与任何运行路径（活跃代码对本目录**零引用**，可用
`grep -r "from archive\|import archive" --include=*.py` 复核）。
保留的目的是回溯当时的实现思路，例如：

| 文件 | 原用途 |
|---|---|
| `ocr_utils.py` / `perception.py` / `a11y_probe.py` | 屏幕读取通道（OCR / UI Automation），v4.1.0 起改为纯 DOM |
| `image_gen.py` / `image_gen_pkg/` | 配图生成 |
| `kb_manager.py` / `meta_learner.py` | 知识库与元学习 |
| `collect_author.py` / `collect_story.py` / `extractors.py` | 早期采集与提取实现 |
| `aizex.py` / `exp_rich_paste.py` / `debug_legacy.py` | 实验与调试脚本 |
| `test_*.py` | 上述模块的旧测试（不在 `tests/run_all.py` 的收集范围内） |

## 约定

- **只读**：不再修改、不再维护、不纳入测试；
- 依赖的第三方库（如 OCR 相关）已从 `requirements.txt` 移除，
  这些文件可能无法直接运行；
- 如需恢复某项能力，请新建实现，不要在此目录上继续改。
