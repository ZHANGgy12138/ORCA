# ORCA 0.9.1 Windows 版附带的运行时与资源

## Python 运行时

Windows 版附带 CPython 3.12。运行时随附的版权声明、PSF 许可和第三方许可收录于 [CPython/LICENSE.txt](CPython/LICENSE.txt)。[Python 官方许可说明](https://docs.python.org/3.12/license.html)

## PDF 版面分析模型

ORCA 使用 DocLayout-YOLO 识别 PDF 的页面布局。随包的 `doclayout_yolo_docstructbench_imgsz1024.onnx` 由 wybxc 从 juliozhao 发布的 DocStructBench 模型转换而来。原模型与 ONNX 版本的发布者都标注了 Apache License 2.0。随包文件的 SHA-256 为 `fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21`。[许可全文](DocLayout-YOLO/LICENSE.txt) · [原始模型](https://huggingface.co/juliozhao/DocLayout-YOLO-DocStructBench) · [ONNX 模型与校验值](https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/blob/89c9fe9/doclayout_yolo_docstructbench_imgsz1024.onnx)

## PDF 译文排版字体

ORCA 随 BabelDOC 资源分发 GoNotoKurrent、Klee One、霞鹜文楷、MaruBuri、Noto Sans、Noto Serif、Source Han Sans 和 Source Han Serif。这些字体使用 SIL Open Font License 1.1。按文件列出的版权与保留字体名称见 [字体版权声明](FONT_NOTICES.txt)。[OFL 1.1 全文](fonts/OFL-1.1.txt) · [BabelDOC 字体资源](https://huggingface.co/datasets/awwaawwa/BabelDOC-Assets/tree/main/fonts)

MaruBuri-Regular.ttf 由 NAVER Corporation 和 NAVER Cultural Foundation 提供。NAVER 在其许可声明中将 MaruBuri 列为保留字体名称。[NAVER 字体来源](https://hangeul.naver.com/font) · [NAVER 字体许可与保留名称](https://help.naver.com/service/30016/contents/18088?osType=PC&lang=ko)

用于 PDF 阅读界面的 PDF.js 及其字体、字符映射和解码器另附各自的许可。[PDF.js 资源清单](pdfjs/PROVENANCE.json)
