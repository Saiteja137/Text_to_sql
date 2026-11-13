from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS departments (
    department_id INTEGER PRIMARY KEY,
    department_name TEXT NOT NULL UNIQUE,
    office_location TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    employee_id INTEGER PRIMARY KEY,
    employee_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    salary REAL NOT NULL CHECK (salary >= 0),
    hire_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    student_id INTEGER PRIMARY KEY,
    student_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    program TEXT NOT NULL,
    enrollment_year INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    course_id INTEGER PRIMARY KEY,
    course_name TEXT NOT NULL,
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    credits INTEGER NOT NULL CHECK (credits > 0)
);

CREATE TABLE IF NOT EXISTS enrollments (
    enrollment_id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(student_id),
    course_id INTEGER NOT NULL REFERENCES courses(course_id),
    semester TEXT NOT NULL,
    grade TEXT,
    UNIQUE (student_id, course_id, semester)
);
"""

SEED_SQL = """
INSERT OR IGNORE INTO departments VALUES
    (1, 'Artificial Intelligence', 'Engineering Block A'),
    (2, 'Computer Science', 'Engineering Block B'),
    (3, 'Mathematics', 'Science Block 1'),
    (4, 'Business', 'Commerce Block'),
    (5, 'Student Services', 'Administration Block');

INSERT OR IGNORE INTO employees VALUES
    (1, 'Aarav Mehta', 'aarav.mehta@example.edu', 1, 128000, '2019-03-11'),
    (2, 'Maya Sharma', 'maya.sharma@example.edu', 1, 116000, '2020-07-06'),
    (3, 'Noah Patel', 'noah.patel@example.edu', 1, 104000, '2022-01-17'),
    (4, 'Emma Chen', 'emma.chen@example.edu', 2, 112000, '2018-09-10'),
    (5, 'Liam Garcia', 'liam.garcia@example.edu', 2, 97000, '2021-05-24'),
    (6, 'Sophia Brown', 'sophia.brown@example.edu', 3, 91000, '2017-08-14'),
    (7, 'Ethan Wilson', 'ethan.wilson@example.edu', 3, 86000, '2023-02-13'),
    (8, 'Olivia Davis', 'olivia.davis@example.edu', 4, 99000, '2020-10-19'),
    (9, 'Lucas Martin', 'lucas.martin@example.edu', 5, 72000, '2022-11-07'),
    (10, 'Isabella Lee', 'isabella.lee@example.edu', 2, 108000, '2019-06-03');

INSERT OR IGNORE INTO students VALUES
    (101, 'Amira Khan', 'amira.khan@example.edu', 'Artificial Intelligence', 2023),
    (102, 'Oliver Smith', 'oliver.smith@example.edu', 'Computer Science', 2022),
    (103, 'Priya Nair', 'priya.nair@example.edu', 'Mathematics', 2024),
    (104, 'Jack Taylor', 'jack.taylor@example.edu', 'Artificial Intelligence', 2021),
    (105, 'Zara Ali', 'zara.ali@example.edu', 'Business Analytics', 2023),
    (106, 'Leo Anderson', 'leo.anderson@example.edu', 'Computer Science', 2024),
    (107, 'Anaya Rao', 'anaya.rao@example.edu', 'Artificial Intelligence', 2022),
    (108, 'Mason Thomas', 'mason.thomas@example.edu', 'Mathematics', 2021),
    (109, 'Isha Gupta', 'isha.gupta@example.edu', 'Business Analytics', 2024),
    (110, 'James White', 'james.white@example.edu', 'Computer Science', 2023);

INSERT OR IGNORE INTO courses VALUES
    (201, 'Introduction to Machine Learning', 1, 4),
    (202, 'Natural Language Processing', 1, 3),
    (203, 'Database Systems', 2, 4),
    (204, 'Algorithms', 2, 4),
    (205, 'Applied Statistics', 3, 3),
    (206, 'Business Intelligence', 4, 3);

INSERT OR IGNORE INTO enrollments VALUES
    (1, 101, 201, 'Fall 2025', 'A'),
    (2, 101, 202, 'Fall 2025', 'A-'),
    (3, 102, 203, 'Fall 2025', 'B+'),
    (4, 102, 204, 'Fall 2025', 'A'),
    (5, 103, 205, 'Fall 2025', 'A'),
    (6, 104, 201, 'Fall 2025', 'B'),
    (7, 105, 206, 'Fall 2025', 'A-'),
    (8, 106, 203, 'Fall 2025', 'B'),
    (9, 107, 202, 'Fall 2025', 'A'),
    (10, 108, 205, 'Fall 2025', 'B+'),
    (11, 109, 206, 'Fall 2025', 'A'),
    (12, 110, 204, 'Fall 2025', 'B+');
"""


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        connection.executescript(SEED_SQL)
        connection.commit()
    finally:
        connection.close()


def _readonly_uri(path: Path) -> str:
    absolute_path = path.resolve()
    return f"file:{quote(str(absolute_path).replace(chr(92), '/'), safe='/:')}?mode=ro"


def connect_readonly(path: Path, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _readonly_uri(path),
        uri=True,
        timeout=timeout_seconds,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}")
    return connection


def get_schema(path: Path) -> str:
    connection = connect_readonly(path)
    try:
        tables = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        if not tables:
            raise RuntimeError(f"No application tables found in database: {path}")

        descriptions: list[str] = []
        for table_row in tables:
            table_name = str(table_row["name"])
            escaped_name = table_name.replace('"', '""')
            columns = connection.execute(f'PRAGMA table_info("{escaped_name}")').fetchall()
            foreign_keys = connection.execute(
                f'PRAGMA foreign_key_list("{escaped_name}")'
            ).fetchall()
            column_lines = [
                f"  - {column['name']} {column['type'] or 'UNDECLARED TYPE'}"
                + (" PRIMARY KEY" if column["pk"] else "")
                + (" NOT NULL" if column["notnull"] else "")
                for column in columns
            ]
            relation_lines = [
                f"  - {item['from']} references {item['table']}.{item['to']}"
                for item in foreign_keys
            ]
            descriptions.append(
                f"Table {table_name}:\n"
                + "\n".join(column_lines)
                + ("\nRelationships:\n" + "\n".join(relation_lines) if relation_lines else "")
            )
        return "\n\n".join(descriptions)
    finally:
        connection.close()


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value
