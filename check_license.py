import os
import sys

LICENSE_HEADER = """Copyright (c) 2024 Isaac Adams
Licensed under the MIT License. See LICENSE file in the project root for full license information."""


def strip_comments(content):
    lines = content.split('\n')
    stripped_lines = [line.lstrip('# ').lstrip('//').lstrip('<!-- ').rstrip(' -->').strip() for line in lines]
    return '\n'.join(stripped_lines)


def check_license(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
        stripped_content = strip_comments(content)
        if LICENSE_HEADER not in stripped_content:
            return False
    return True


def main():
    failed_files = []
    for root, dirs, files in os.walk('.'):
        # Skip node_modules directory
        dirs[:] = [d for d in dirs if d != 'node_modules']
        for file in files:
            if file.endswith(('.py', '.ts', '.html', '.tsx', '.yml', '.md')):
                file_path = os.path.join(root, file)
                if not check_license(file_path):
                    failed_files.append(file_path)

    if failed_files:
        print("The following files are missing the license header:")
        for file in failed_files:
            print(file)
        sys.exit(1)
    else:
        print("All files have the license header.")


if __name__ == "__main__":
    main()