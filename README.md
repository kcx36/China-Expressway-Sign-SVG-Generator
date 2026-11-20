# China-Expressway-Sign-SVG-Generator

生成道路标志（SVG/PNG格式），支持国家高速、省级高速、国道、省道、县道、乡道，样式遵循《[道路交通标志和标线 第2部分：道路交通标志](https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=15B1FC09EE1AE92F1A9EC97BA3C9E451)》（GB5768.2-2022）。

高速标志仅支持有限种的编号位数、名称字数组合。

## 使用前准备

1. [Inkscape](https://inkscape.org/)，用于文字转曲和导出PNG。程序会自动查找安装位置，或者手动指定`inkscape.exe`路径。
2. A型交通标志专用字体、B型交通标志专用字体，仓库中的B型字体修正了数字“3”的左右间距，官方原版字体可在[此处](https://xxgk.mot.gov.cn/jigou/glj/202006/t20200623_3312662.html)下载。
