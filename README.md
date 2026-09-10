# MMY RenderCleaner

渲染序列帧透明区抖动清洗工具 —— 单文件 Python GUI，零安装依赖（除 Pillow/numpy）。

## 背景

Blender 色彩管理的 Dither（抖动，默认 1.0）输出 PNG 时会给**整图（包括 alpha=0 的透明区）**加 ±1 级随机噪声：

- 肉眼完全不可见（alpha=0 不参与显示）
- 但彻底破坏 PNG 无损压缩，序列帧体积虚胖 **约 6 倍**
- 实测：875×875 单帧 382KB → 清洗后 51KB；整目录 1.2GB → 69MB（**-94%**）

> 2026-09-09 起 mmy_3Dto2D_Sprites 插件渲染入口已统一 dither=0（PR #59），
> 新产出不会再有此问题；本工具用于清洗**存量**与美术手渲的旧工程输出。

## 使用

```
双击 / python render_cleaner.py        # GUI 模式
python render_cleaner.py --cli <根目录> [--no-backup] [--dry-run]   # 命令行模式
```

1. 选择根目录（支持任意层级：hero 总目录 / 角色目录 / Render_Output 均可）——
   也可以**直接把文件夹拖进窗口**（Windows 原生拖放，拖入后自动扫描；拖入文件则用其所在目录）
2. 点「扫描」—— 自动识别清洗单元（一级含 PNG 的子目录），抽样检测噪声，红色=发现噪声
3. 勾选要处理的单元 →「开始清洗（选中项）」
4. 完成后自动复检，日志显示 ✅/❌

### 原理与安全性

- 只把 **alpha==0 像素的 RGB 置 0**，角色本体像素一个不动，画面 100% 无损
- 默认清洗前把原目录**复制备份**到单元同级 `_dither_backup_时间戳/`（可取消勾选）
- 备份目录在后续扫描/清洗中自动排除，不会被误收进 SVN 产出
- 仅处理 PNG（JPG/BMP 无 alpha，无需处理）

### 清洗单元识别规则

| 选择的目录 | 识别出的单元 |
|---|---|
| hero 总目录 | 每个角色目录 |
| 角色目录 | Render_Output / Bake / Tex 等含 PNG 的子目录 |
| Render_Output | 各变体/部件子目录 |

建议按角色目录清洗（只勾 Render_Output），避免动到 Bake/原画等。

## 依赖

- Python 3.10+（Windows 自带 tkinter）
- `pip install pillow numpy`

## 打包单文件 exe

```
pip install pyinstaller
pyinstaller -F -w -n MMY-RenderCleaner --icon=icon.ico render_cleaner.py
```

已打好的一份在 `dist/MMY-RenderCleaner.exe`（28MB，免 Python 环境直接双击运行）。
config.json 会生成在 exe 同目录，记住上次选择的目录。
