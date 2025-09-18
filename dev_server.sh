#!/bin/bash

DEBUG=true uvicorn indexer_utils.main:app --reload --reload-exclude=.venv --host 0.0.0.0 --port 8000
