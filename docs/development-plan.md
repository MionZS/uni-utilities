# Development Plan: File Name Formatter CLI Tool

## Overview
Evolve the current `file_name_formatter.py` script into a robust, installable CLI utility for formatting strings into filename-safe formats with advanced features.

## Current State
- Interactive loop for continuous formatting
- Basic cleaning (whitespace, invalid chars)
- Snake_case conversion
- Clipboard copying
- Colored output
- 7-second timed erase with cursor repositioning

## Goals
Create a flexible tool that doesn't exist in the ecosystem, combining case conversion, clipboard integration, and unique erase behavior.

## Planned Features

### 1. Command-Line Interface (argparse)
- `--input` / `-i`: Direct input string
- `--case` / `-c`: Case style (snake, camel, kebab, pascal, flat)
- `--loop`: Interactive loop mode
- `--timeout`: Erase delay (seconds)
- `--no-color`: Disable colors
- `--no-clipboard`: Skip clipboard

### 2. Case Conversion Support
Using `caseconverter` library:
- snake_case (default)
- camelCase
- kebab-case
- PascalCase
- flatcase

### 3. Operating Modes
- **Single**: Format one string and exit
- **Loop**: Continuous interactive mode
- **Prompt**: One-time interactive (default)

### 4. Configuration Options
- Custom erase timeouts
- Color toggling
- Clipboard toggling
- Error handling

### 5. Packaging & Distribution
- Entry point for `pip install`
- Global command `file-formatter`
- pyproject.toml updates

### 6. Advanced Features (Future)
- Custom delimiters
- File batch processing
- GUI mode
- API endpoints

## Implementation Roadmap

### Phase 1: Core CLI
1. Add argparse to main()
2. Import caseconverter functions
3. Implement case selection
4. Add mode logic (single/loop/prompt)

### Phase 2: Options & Polish
1. Timeout configuration
2. Color/clipboard toggles
3. Input validation
4. Error messages

### Phase 3: Packaging
1. Update pyproject.toml
2. Add entry points
3. Test installation
4. Documentation

### Phase 4: Extras
1. Unit tests
2. CI/CD setup
3. Release on PyPI

## Usage Examples
```bash
# Single format
file-formatter --input "Hello World" --case camel

# Loop with custom timeout
file-formatter --loop --case kebab --timeout 3

# No colors, no clipboard
file-formatter --input "test" --no-color --no-clipboard
```

## Dependencies
- pyperclip
- colorama
- case-converter
- (future: argparse built-in)

## Risks & Considerations
- Terminal compatibility for erase/reposition
- Performance in loop mode
- Clipboard availability across platforms

## Success Criteria
- Passes all planned use cases
- Installable via pip
- Clean, documented code
- No existing tool duplication