from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection

import aiofiles
import click
import exifread
import pytz

from media_vault.__about__ import __version__

DB_NAME = "media-vault.db"


@click.group()
def mvu():
    pass


@mvu.command()
def hello():
    click.echo("Hello!!!")


@mvu.command()
def init():
    """Initialize the project."""
    # Path to the SQLite database
    db_path = Path(DB_NAME)

    # Check if the database file already exists
    if os.path.exists(db_path):
        click.echo("Database already exists. Initialization failed.")
        return

    # Create and initialize the SQLite database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create the version table
    cursor.execute("""
    CREATE TABLE version (
        version TEXT
    )
    """)

    # Insert the current version into the version table
    cursor.execute(
        """
    INSERT INTO version (version) VALUES (?)
    """,
        (__version__,),
    )

    # Create the files table
    cursor.execute("""
    CREATE TABLE files (
        md5_hash TEXT,
        current_name TEXT,
        original_name TEXT,
        original_path TEXT,
        original_created_at TIMESTAMP,
        current_created_at TIMESTAMP,
        metadata JSON
    )
    """)
    # Commit the changes and close the connection
    conn.commit()
    conn.close()
    click.echo("Project initialized.")


@mvu.command()
@click.option("--source", type=click.Path(exists=True), help="Path to source folder")
@click.option("--concurrency", type=int, default=10, help="Number of concurrently processed files")
def backup(source, concurrency):
    """Backup media files from the source folder and its subfolders to the current folder."""

    total_files = 0
    processed_files = 0
    transferred_bytes = 0

    async def process_file(file_path: Path, dest_dir: Path, conn: Connection, semaphore: asyncio.Semaphore) -> None:
        nonlocal processed_files, transferred_bytes
        async with semaphore:
            click.echo(f"Starting processing file: {file_path}")

            # Compute MD5 hash
            async with aiofiles.open(file_path, "rb") as f:
                file_data = await f.read()
                md5_hash = hashlib.md5(file_data).hexdigest()  # noqa: S324

            # Check if the MD5 hash is already in the files table
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM files WHERE md5_hash = ?", (md5_hash,))
            if cursor.fetchone()[0] > 0:
                click.echo(f"Skipping file (already exists): {file_path}")
                return  # Skip the file if it already exists in the database

            # Extract EXIF metadata
            exif_data = exifread.process_file(io.BytesIO(file_data))
            exif_metadata = {tag: str(value) for tag, value in exif_data.items()}

            # Get file creation date and time from EXIF or fallback to file system
            original_created_at = exif_metadata.get("EXIF DateTimeOriginal")
            if not original_created_at:
                original_created_at = datetime.fromtimestamp(os.path.getctime(file_path), tz=pytz.UTC).isoformat()
            else:
                original_created_at = (
                    datetime.strptime(original_created_at, "%Y:%m:%d %H:%M:%S").replace(tzinfo=pytz.UTC).isoformat()
                )

            original_created_at = original_created_at.replace(":", "-").replace(" ", "T")

            new_filename = f"{original_created_at}_{md5_hash}{os.path.splitext(file_path)[1]}".replace(" ", "_")

            # Copy file to the destination directory
            dest_path = os.path.join(dest_dir, new_filename)
            async with aiofiles.open(file_path, "rb") as src, aiofiles.open(dest_path, "wb") as dest:
                await dest.write(await src.read())

            # Update statistics
            processed_files += 1
            transferred_bytes += len(file_data)

            # Insert file info into the database
            cursor.execute(
                """
            INSERT INTO files (md5_hash, current_name, original_name, original_path, original_created_at, current_created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    md5_hash,
                    new_filename,
                    os.path.basename(file_path),
                    os.path.abspath(file_path),
                    original_created_at,
                    datetime.now(tz=pytz.UTC).isoformat(),
                    json.dumps({"exif": exif_metadata}),
                ),
            )
            conn.commit()

            click.echo(
                f"File processed and copied: {file_path} as {new_filename} (Processed {processed_files}/{total_files})"
            )

    async def process_directory(source_dir: Path, dest_dir: Path, conn: Connection, concurrency: int):
        nonlocal total_files
        semaphore = asyncio.Semaphore(concurrency)  # Limit the number of concurrent tasks
        tasks = []
        for root, _, files in os.walk(source_dir):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png", ".mp4", ".avi", ".mov")):
                    file_path = Path(os.path.join(root, file))
                    tasks.append(process_file(file_path, dest_dir, conn, semaphore))
                    total_files += 1
        await asyncio.gather(*tasks)

    # Connect to the SQLite database
    db_path = os.path.join(os.getcwd(), DB_NAME)
    conn = sqlite3.connect(db_path)

    # Destination directory for backups
    dest_dir = Path(os.getcwd())

    # Start processing the files
    click.echo(f"Starting backup from {source} to {dest_dir} with concurrency level: {concurrency}")
    asyncio.run(process_directory(Path(source), dest_dir, conn, concurrency))
    click.echo(f"Media files backed up from {source} to {dest_dir}")
    click.echo(
        f"Total files: {total_files}, Processed files: {processed_files}, Transferred bytes: {transferred_bytes}"
    )


if __name__ == "__main__":
    mvu()
