#
#  This is the withhacks setuptools script.
#  Originally developed by Ryan Kelly, 2009.
#
#  This script is placed in the public domain.
#

from setuptools import setup

with open('withhacks/__about__.py') as f:
    exec(f.read())

VERSION = __version__

NAME = "withhacks"
DESCRIPTION = "building blocks for with-statement-related hackery"
with open('README.txt') as f:
    LONG_DESC = f.read()
AUTHOR = "Ryan Kelly"
AUTHOR_EMAIL = "ryan@rfk.id.au"
URL = "http://github.com/rfk/withhacks"
LICENSE = "MIT"
KEYWORDS = "context manager with statement"

setup(name=NAME,
      version=VERSION,
      author=AUTHOR,
      author_email=AUTHOR_EMAIL,
      url=URL,
      description=DESCRIPTION,
      long_description=LONG_DESC,
      license=LICENSE,
      keywords=KEYWORDS,
      packages=["withhacks","withhacks.tests"],
      install_requires=['bytecode'],
      test_suite='withhacks.tests'
     )

