# Role
你是一名精通现代 WebGL (Three.js/自定义着色器)、无人机刚体动力学以及高级数学（李群/李代数）的资深图形学与物理引擎程序员。

# Goal
请编写一个单文件网页版（HTML + CSS + JS + GLSL）的【FPV 穿越机花飞模拟器】。
该程序必须具备极高真实度的飞行物理、标准的遥控器硬件接入能力，以及基于距离衰减的模拟图传视觉效果。代码必须完全闭合且无需额外配置即可运行。

# Core Physics & Control (核心物理与控制管线)
本模块用于严格评估你的数学建模与物理积分能力：

1. 硬件输入映射 (USB HID Gamepad)：
   - 使用 Web Gamepad API 实时读取连接到电脑的开源系统遥控器（如基于 OpenTX/EdgeTX 的摇杆设备）。
   - 提取 4 个核心通道（Pitch, Roll, Yaw, Throttle），并将其归一化到 $[-1, 1]$（油门为 $[0, 1]$）的数据区间。

2. 姿态积分与 SO(3) 李代数 (Advanced Rigid Body Dynamics)：
   - 彻底摒弃容易产生万向节死锁的欧拉角积分。
   - 穿越机的姿态必须使用三维旋转群 $\text{SO}(3)$ 表示。角速度更新必须通过其李代数 $\mathfrak{so}(3)$ 的指数映射（Exponential Map）来完成精确的姿态积分：$\mathbf{R}_{t+\Delta t} = \mathbf{R}_t \exp(\hat{\omega} \Delta t)$。
   - 实现包含重力、空气阻尼（与速度平方成正比）以及电机总推力的平动动力学方程。

3. Acro 模式与 PID 闭环 (Flight Controller)：
   - 实现 FPV 经典的 Acro（手动/纯角速度）模式。遥控器摇杆的输入不控制绝对角度，而是映射为**目标角速度**（Target Angular Velocity）。
   - 编写一个三轴 PID 控制器，计算当前角速度与目标角速度的误差，并输出虚拟的力矩（Torque）作用于刚体动力学中。

# Graphics & World Generation (渲染与环境构建)
本模块评估你的 Three.js 场景搭建与材质应用能力：

1. 简易世界盒：
   - 地面：生成一个巨大的平面，并使用程序化生成的（或通过 DataURI 注入的）草地纹理进行大面积平铺（RepeatWrapping）。
   - 天空：应用一张基础的 HDRI（全景 RGBE 或等效的渐变天空盒）提供全局光照和环境反射。
2. 花飞场地 (Freestyle Track)：
   - 在中心区域附近，使用极简的 Three.js 几何体生成一个极具挑战性的训练场。
   - 必须包含：多个环形门（TorusGeometry）、几道可供“钻越”的桥梁或废墟墙（BoxGeometry 的组合）、以及可供绕柱飞行的垂直立柱（CylinderGeometry）。添加高对比度的颜色以辅助视觉定位。

# VTX Degradation Shader (模拟图传衰弱后处理)
本模块评估你的 GLSL Fragment Shader 编写能力：
- 玩家的操控范围受限于“模拟信号强度”。信号强度 $S$ 与无人机距离起飞原点（或接收天线）的距离 $D$ 成反比。
- 必须使用 WebGL / Three.js EffectComposer 编写一个全屏后处理着色器，模拟真实的 5.8G VTX 模拟图传衰弱效果：
  - 当距离适中时：画面开始出现随机的横向雪花纹（Static Noise）和色彩饱和度降低。
  - 当距离到达危险边缘时：画面产生严重的横向撕裂（Horizontal Tearing）、不同步的黑色滚动条纹（Rolling Bands），并伴随极高的白噪声。
  - 彻底失联：若超出极限距离，画面完全变为雪花点，穿越机失去推力坠毁。

# Technical Constraints (技术限制)
- 所有逻辑必须包含在一个 `.html` 文件中。允许通过 CDN 引入 `three.min.js` 以及其扩展库（如 `EffectComposer`）。
- 严禁使用任何第三方物理引擎（如 Cannon.js/Ammo.js），所有的无人机动力学与 SO(3) 积分运算必须由你从零手写实现。
