import setuptools
from mautrix_asmux import __version__

with open("requirements.txt") as reqs:
    install_requires = reqs.read().splitlines()

try:
    long_desc = open("README.md").read()
except IOError:
    long_desc = "Failed to read README.md"

setuptools.setup(
    name="mautrix-asmux",
    version=__version__,
    url="https://github.com/beeper/mautrix-asmux",

    author="Tulir Asokan",
    author_email="tulir@maunium.net",

    description="A Matrix application service proxy and multiplexer",
    long_description=long_desc,
    long_description_content_type="text/markdown",

    packages=setuptools.find_packages(),

    install_requires=install_requires,
    python_requires="~=3.9",

    classifiers=[
        "Development Status :: 7 - Inactive",
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Communications :: Chat",
        "Framework :: AsyncIO",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
    ],
    package_data={"mautrix_asmux": ["example-config.yaml"]},
)
