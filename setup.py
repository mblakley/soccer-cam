from setuptools import setup, find_packages

setup(
    name="video_grouper",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.24.0",
        "aiofiles>=23.1.0",
    ],
    entry_points={
        "console_scripts": [
            "video-grouper=video_grouper.__main__:main_entry",
        ],
    },
    python_requires=">=3.8",
    author="Mark B",
    description="A tool for managing and processing soccer game recordings from IP cameras",
    keywords="video, camera, soccer, recording",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: End Users/Desktop",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
) 