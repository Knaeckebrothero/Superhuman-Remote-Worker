#!/usr/bin/env python3
"""Convert a Neo4j Cypher export from CREATE to MERGE mode.

This converts:
- Node CREATE statements to MERGE (matching on unique key, then SET remaining props)
- Relationship CREATE statements to MERGE

Usage:
    python scripts/convert_to_merge.py data/original_data.cypher data/original_merge.cypher
"""
import re
import sys
from pathlib import Path


def get_unique_key(label: str, props_str: str) -> tuple[str, str, str]:
    """Extract unique key from properties string.
    
    Returns: (key_name, key_value, remaining_props_str)
    """
    # Priority order for unique keys
    key_priority = ['xmi_id', 'attr_id', 'vs_id', 'type_key', 'name']
    
    # Parse properties - simple regex for key: value pairs
    # Match patterns like: key: 'value' or key: 123 or key: true
    prop_pattern = r"(\w+):\s*('(?:[^'\\]|\\.)*'|\d+(?:\.\d+)?|true|false|null|\[.*?\])"
    props = dict(re.findall(prop_pattern, props_str))
    
    # Find unique key
    for key in key_priority:
        if key in props:
            remaining = {k: v for k, v in props.items() if k != key}
            return key, props[key], remaining
    
    # Fallback to first property
    if props:
        first_key = list(props.keys())[0]
        remaining = {k: v for k, v in props.items() if k != first_key}
        return first_key, props[first_key], remaining
    
    return None, None, {}


def convert_node_create_to_merge(line: str) -> str:
    """Convert a CREATE node statement to MERGE."""
    # Match: CREATE (:Label {props});
    match = re.match(r'^CREATE \(:([^\s{]+)\s*\{(.+)\}\);$', line)
    if not match:
        return line
    
    labels = match.group(1)
    props_str = match.group(2)
    
    # Get unique key
    key_name, key_value, remaining = get_unique_key(labels.split(':')[0], props_str)
    
    if key_name is None:
        # Can't find unique key, keep as CREATE
        return line
    
    # Build MERGE statement
    result = f"MERGE (n:{labels} {{{key_name}: {key_value}}})\n"
    if remaining:
        set_parts = ', '.join(f"n.{k} = {v}" for k, v in remaining.items())
        result += f"SET {set_parts};\n"
    else:
        result = result.rstrip('\n') + ";\n"
    
    return result


def convert_rel_create_to_merge(line: str) -> str:
    """Convert a CREATE relationship statement to MERGE."""
    return line.replace('CREATE (a)-[:', 'MERGE (a)-[:')


def convert_file(input_path: Path, output_path: Path):
    """Convert entire file from CREATE to MERGE mode."""
    with open(input_path) as f:
        content = f.read()
    
    lines = content.split('\n')
    output_lines = []
    
    in_relationship_section = False
    
    for line in lines:
        # Track which section we're in
        if '// === RELATIONSHIPS ===' in line:
            in_relationship_section = True
        
        # Update header
        if line.startswith('// Mode:'):
            line = '// Mode: MERGE (idempotent)'
        
        # Convert statements
        if line.startswith('CREATE (:'):
            line = convert_node_create_to_merge(line)
        elif line.startswith('CREATE (a)-[:'):
            line = convert_rel_create_to_merge(line)
        
        output_lines.append(line)
    
    with open(output_path, 'w') as f:
        f.write('\n'.join(output_lines))
    
    print(f"Converted {input_path} -> {output_path}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.cypher> <output.cypher>")
        sys.exit(1)
    
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)
    
    convert_file(input_path, output_path)
