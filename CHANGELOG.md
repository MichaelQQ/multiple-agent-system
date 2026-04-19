# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `MAS_OLLAMA_TIMEOUT` environment variable (default: 3600s) for controlling HTTP request timeout to the Ollama API.

### Fixed

- OllamaAdapter now properly categorizes failure messages by exception type:
  - Connection errors (URLError) produce "connection error" message
  - Timeout errors (socket.timeout) produce "timeout after Xs" message
  - HTTP errors (4xx/5xx responses) produce "HTTP {code}: {reason}" message
  - JSON decode errors produce "invalid JSON response" message
- Failure result objects now properly populate all required schema fields (`task_id`, `status`, `summary`, `artifacts`, `handoff`, `verdict`, `feedback`, `tokens_in`, `tokens_out`, `duration_s`, `cost_usd`).