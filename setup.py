from setuptools import setup, find_packages
from pathlib import Path

setup(
    name="finsight",
    version="0.1.0",
    author="Rohan Khiani",
    # The project's published contact, the same address as README.md and SECURITY.md.
    # It ships in the package metadata.
    author_email="rohan.s.khiani.vora@gmail.com",
    description=(
        "FinSight reads a company's 10-K financials from SEC EDGAR, cites every figure to "
        "its filing, and builds a DCF and a research report from them."
    ),
    long_description=(Path(__file__).parent / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/ROROK20/FinSight",
    packages=find_packages(exclude=["tests*"]),
    # The recorded SEC companyfacts snapshots demo mode runs on.
    package_data={"finsight": ["data/companyfacts/*.json", "data/companyfacts/SHA256SUMS"]},
    python_requires=">=3.11",  # pandas>=3.0.3 / numpy>=2.4.6 floors need 3.11+
    # Floors, not pins: this stays installable alongside other packages. The floors
    # are the oldest versions the suite and pip-audit have run against. For a
    # reproducible install use `pip install -r requirements.txt`, which carries the
    # exact `==` pins.
    install_requires=[
        "yfinance>=1.4.1",
        "pandas>=3.0.3",
        "numpy>=2.4.6",
        "requests>=2.34.2",
        "beautifulsoup4>=4.14.3",
        "fastapi>=0.136.3",
        # FastAPI transitive, floored explicitly: starlette 1.2.1 and older carry
        # PYSEC-2026-248 / PYSEC-2026-249 (fixed in 1.3.0 / 1.3.1) and
        # `fastapi>=0.136` alone permits a vulnerable resolve. Never lower this.
        "starlette>=1.3.1",
        "uvicorn>=0.49.0",
        "python-dotenv>=1.2.2",
    ],
    extras_require={
        # `llm` and `finbert` keep floors: they are not installed in the verified
        # environment and the suite never exercises them
        # (tests/test_sentiment.py builds `SentimentAnalyzer(use_finbert=False)`),
        # so an exact pin would assert a combination this repo has never run.
        "llm": ["openai>=1.12.0"],
        "finbert": ["transformers>=4.36.0", "torch>=2.0.0"],
        # `dev` IS pinned: `pip install -e ".[dev]"` is the documented test and
        # CI command, so its toolchain should be reproducible.
        "dev": ["pytest==9.0.3", "pytest-cov==7.1.0", "httpx==0.28.1"],
    },
    entry_points={
        "console_scripts": [
            "finsight=finsight.api.app:main",
        ],
    },
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial :: Investment",
    ],
)
