import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="miqro",  # Replace with your own username
    version="1.4.0",
    author="Daniel Fett",
    author_email="miqro@danielfett.de",
    description="MIQRO is an MQTT Micro-Service Library for Python",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/danielfett/miqro",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    install_requires=[
        # The v1 callback API is used throughout, and client_id is passed
        # positionally; paho 2.x takes callback_api_version first and would fail
        # at construction.
        "paho-mqtt>=1.6.1,<2",
        "pyyaml",
    ],
    python_requires=">=3.9",
)
