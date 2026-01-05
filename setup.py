import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="miqro",  # Replace with your own username
    version="1.2.2",
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
        "paho.mqtt",
        "pyyaml",
    ],
    python_requires=">=3.6",
)
