
import os

import sys

import io
 
sys.path.insert(0, os.path.dirname(__file__))

os.chdir(os.path.dirname(__file__))
 
os.environ['PYTHONIOENCODING'] = 'utf-8'

os.environ['LANG'] = 'en_US.UTF-8'

os.environ['LC_ALL'] = 'en_US.UTF-8'
 
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
 
from app import app as application

