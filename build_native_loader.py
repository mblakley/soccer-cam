"""Compile video_grouper/ball_tracking/_secure_loader_native.pyx into a
native extension (.pyd / .so / .dylib) and place it next to its source.

Run this before packaging release builds (e.g., before PyInstaller). The
runtime falls back to pure-Python implementations if the compiled module
is missing, so dev/test workflows don't need this step.

Usage:
    uv pip install cython setuptools
    python build_native_loader.py build_ext --inplace
"""

from setuptools import Extension, setup

try:
    from Cython.Build import cythonize
except ImportError as exc:
    raise SystemExit(
        "Cython is required to build the native loader. "
        "Install it with: uv pip install cython setuptools"
    ) from exc


extensions = [
    Extension(
        name="video_grouper.ball_tracking._secure_loader_native",
        sources=["video_grouper/ball_tracking/_secure_loader_native.pyx"],
    ),
]

setup(
    name="video-grouper-native-loader",
    packages=[],
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    ),
)
