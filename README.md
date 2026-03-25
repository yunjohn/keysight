# Keysight 示波器助手

基于 `PySide6 + PyVISA` 的是德示波器桌面工具，用于：

- 连接 Keysight / Agilent 示波器
- 抓取多通道波形并离线复盘
- 单次测量、自动测量
- 独立波形窗口缩放、游标、局部测量
- 启动刹车性能测试与记录导出

当前应用标题为：`Keysight 示波器助手 | 作者：徐`

## 主要功能

- 自动扫描并连接 VISA 资源
- 主界面控制示波器通道开关
- 抓取 `NORMal / MAXimum / RAW` 波形
- 支持高点数波形显示、局部缩放、导出 CSV / PNG
- 独立波形窗口支持：
  - 多通道显示
  - 鼠标框选时间轴缩放
  - A/B 游标
  - 游标联动拖动
  - 按当前视图 / 游标 A-B / 整条波形测量
- 触发区支持：
  - 单次等待触发
  - 触发状态读取
  - `ROLL / 标准模式` 切换
- 启动刹车测试支持：
  - 完整流程 / 仅启动段 / 仅刹车段
  - 电流归零 / A 相回溯
  - 结果点定位
  - 历史记录持久化
  - CSV 统计导出

## 环境要求

- Windows x64
- Python 3.12
- Keysight IO Libraries Suite

建议优先安装：

- `Keysight IO Libraries Suite (IOLS)`

如果没有安装 VISA 运行环境，程序即使能启动，也无法正常连接示波器。

## 安装依赖

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

## 运行

```powershell
.venv\Scripts\python main.py
```

## 打包 EXE

目录版：

```powershell
.venv\Scripts\python -m PyInstaller --noconfirm --clean --windowed --name KeysightScopeApp --icon assets\app.ico --paths src main.py
```

单文件版：

```powershell
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed --name KeysightScopeApp-OneFile --icon assets\app.ico --paths src main.py
```

打包后产物位于：

- `dist\KeysightScopeApp\KeysightScopeApp.exe`
- `dist\KeysightScopeApp-OneFile.exe`

## 使用流程

1. 点击 `刷新资源`，选择示波器资源地址。
2. 点击 `连接设备`。
3. 在主界面设置通道、触发、测量参数。
4. 点击 `抓取波形`，程序会自动打开独立波形窗口。
5. 在独立波形窗口里进行缩放、游标、局部测量和导出。
6. 需要做性能分析时，打开 `启动刹车测试`。

## 波形窗口说明

独立波形窗口顶部支持：

- `抓取波形`
- `重置波形`
- `测量项设置`
- `测量范围`

测量范围有三种：

- `当前视图`：按当前可见时间窗计算
- `游标 A-B`：按两根游标之间的时间窗计算
- `整条波形`：按整条波形计算

## 触发说明

- `ROLL` 模式下，边沿触发不可用
- 可先切换到 `标准模式` 再执行 `单次等待触发`
- `单次等待触发` 会自动应用当前界面里的触发参数

## 启动刹车测试说明

- 执行测试时优先抓取最新波形
- 不会自动弹出波形窗口
- 只有点击结果点 `定位` 或手动应用游标时，才会弹出独立波形窗口
- 测试记录支持本地持久化与 CSV 导出

## 目录说明

- `src/keysight_scope_app/`：主程序代码
- `tests/`：回归测试
- `assets/`：图标等资源
- `captures/`：运行期抓图、波形、测试记录

## 测试

语法检查：

```powershell
.venv\Scripts\python -m py_compile src\keysight_scope_app\ui\main_window.py
```

示例回归：

```powershell
.venv\Scripts\python -m pytest tests\test_utils.py -k startup_brake
```

## 说明

- 不同 Keysight 系列的 SCPI 支持会有差异
- 当前实现优先兼容 InfiniiVision 常见命令集
- 部分功能依赖示波器当前采集状态、时基模式和通道配置
