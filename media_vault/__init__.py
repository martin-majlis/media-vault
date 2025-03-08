from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection
from typing import Optional

import aiofiles
import click
import exifread
import pytz

from media_vault.__about__ import __version__

DB_NAME = ".media-vault.db"


@click.group()
def mvu():
    pass


@mvu.command()
def version():
    click.echo(__version__)


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
    cursor.execute(
        """
    CREATE TABLE version (
        version TEXT
    )
    """
    )

    # Insert the current version into the version table
    cursor.execute(
        """
    INSERT INTO version (version) VALUES (?)
    """,
        (__version__,),
    )

    # Create the files table
    cursor.execute(
        """
    CREATE TABLE files (
        md5_hash TEXT PRIMARY KEY,
        name TEXT,
        created_at TIMESTAMP,
        metadata JSON,
        size INTEGER
    )
    """
    )

    # Create the original_files table
    cursor.execute(
        """
    CREATE TABLE original_files (
        md5_hash TEXT,
        full_path TEXT,
        name TEXT,
        created_at TIMESTAMP,
        modified_at TIMESTAMP,
        metadata JSON,
        size INTEGER,
        UNIQUE(full_path, modified_at, size)
    )
    """
    )

    cursor.execute("CREATE INDEX idx_md5_hash ON original_files (md5_hash);")

    # Commit the changes and close the connection
    conn.commit()
    conn.close()
    click.echo("Project initialized.")


from enum import Enum


class CheckMode(Enum):
    MODIFIED_AT = "modified_at"
    HASH = "hash"


@mvu.command()
@click.option("--source", type=click.Path(exists=True), help="Path to source folder")
@click.option(
    "--concurrency", type=int, default=10, help="Number of concurrently processed files"
)
@click.option("--from-index", type=int, help="Start processing from this file index")
@click.option(
    "--from-date",
    type=click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]),
    help="Process files created on or after this date",
)
@click.option("--limit", type=int, help="Limit the number of files to process")
@click.option(
    "--check",
    type=click.Choice([e.value for e in CheckMode]),
    default=CheckMode.MODIFIED_AT.value,
    help="Check mode for file processing",
)
def backup(
    source,
    concurrency,
    from_index: int | None,
    from_date: datetime | None,
    limit: int | None,
    check: str,
):
    """Backup media files from the source folder and its subfolders to the current folder."""
    check_mode = CheckMode(check)
    total_files = 0
    processed_files = 0
    skipped_files = 0
    transferred_bytes = 0

    async def process_file(
        file_path: Path,
        dest_dir: Path,
        conn: Connection,
        semaphore: asyncio.Semaphore,
        check_mode: CheckMode,
    ) -> None:
        nonlocal processed_files, skipped_files, transferred_bytes
        async with semaphore:
            click.echo(
                f"Stats - Total: {total_files}; Proc: {processed_files}; Skip: {skipped_files} - ({processed_files + skipped_files} / {total_files})"
            )
            click.echo(f"Starting processing file: {file_path}")

            cursor = conn.cursor()

            modified_at = datetime.fromtimestamp(
                os.path.getmtime(file_path), tz=pytz.UTC
            ).isoformat()
            file_size = os.path.getsize(file_path)

            if check_mode == CheckMode.MODIFIED_AT:
                cursor.execute(
                    """
                    SELECT 1 FROM original_files WHERE full_path = ? AND modified_at = ? AND size = ?
                    """,
                    (str(file_path.absolute()), modified_at, file_size),
                )
                if cursor.fetchone():
                    skipped_files += 1
                    click.echo(
                        f"Skipping file (already processed based on modified_at): {file_path}"
                    )
                    return

            # Compute MD5 hash
            async with aiofiles.open(file_path, "rb") as f:
                file_data = await f.read()
                md5_hash = hashlib.md5(file_data).hexdigest()  # noqa: S324

            cursor.execute(
                """
                SELECT 1 FROM original_files WHERE md5_hash = ?
                """,
                (md5_hash,),
            )
            if cursor.fetchone():
                skipped_files += 1
                click.echo(
                    f"Skipping file (already processed based on hash): {file_path}"
                )
                return

            # Try to insert into original_files
            try:
                cursor.execute(
                    """
                    INSERT INTO original_files (md5_hash, full_path, name, created_at, modified_at, metadata, size)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        md5_hash,
                        str(file_path.absolute()),
                        file_path.name,
                        datetime.fromtimestamp(
                            os.path.getctime(file_path), tz=pytz.UTC
                        ).isoformat(),
                        modified_at,
                        json.dumps(
                            {
                                "file": {
                                    "size": file_size,
                                    "type": file_path.suffix[1:],
                                }
                            }
                        ),
                        file_size,
                    ),
                )
            except sqlite3.IntegrityError:
                # File already exists in original_files, skip processing
                conn.rollback()
                skipped_files += 1
                click.echo(f"Skipping file (already processed): {file_path}")
                return

            # If we're here, it's a new file, so let's process it

            # Extract EXIF metadata
            exif_data = exifread.process_file(io.BytesIO(file_data))
            exif_metadata = {tag: str(value) for tag, value in exif_data.items()}

            # Get file creation date and time from EXIF or fallback to file system
            created_at = exif_metadata.get("EXIF DateTimeOriginal")
            if not created_at:
                created_at = datetime.fromtimestamp(
                    os.path.getctime(file_path), tz=pytz.UTC
                ).isoformat()
            else:
                created_at = (
                    datetime.strptime(created_at, "%Y:%m:%d %H:%M:%S")
                    .replace(tzinfo=pytz.UTC)
                    .isoformat()
                )

            created_at = created_at.replace(":", "-").replace(" ", "T")

            new_filename = f"{created_at}_{md5_hash}{file_path.suffix}".replace(
                " ", "_"
            )

            # Copy file to the destination directory
            dest_path = dest_dir / new_filename
            async with aiofiles.open(dest_path, "wb") as dest:
                await dest.write(file_data)
            # preserve statistics
            shutil.copystat(file_path, dest_path)

            # Update statistics
            processed_files += 1
            transferred_bytes += file_size

            # Try to insert file info into the files table
            try:
                cursor.execute(
                    """
                    INSERT INTO files (md5_hash, name, created_at, metadata, size)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        md5_hash,
                        new_filename,
                        created_at,
                        json.dumps({"exif": exif_metadata}),
                        file_size,
                    ),
                )
            except sqlite3.IntegrityError:
                # This shouldn't happen often, but handle it just in case
                click.echo(
                    f"Warning: File with hash {md5_hash} already exists in files table."
                )

            conn.commit()

            click.echo(f"File processed and copied: {file_path} as {new_filename}")

    async def process_directory(
        source_dir: Path,
        dest_dir: Path,
        conn: Connection,
        concurrency: int,
        from_index: int | None,
        from_date: datetime | None,
        limit: int | None,
    ):
        nonlocal total_files, processed_files
        semaphore = asyncio.Semaphore(concurrency)

        all_files = []
        for root, _, files in os.walk(source_dir):
            for file in files:
                if file.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".mp4", ".avi", ".mov")
                ):
                    file_path = Path(os.path.join(root, file))
                    created_time = datetime.fromtimestamp(
                        os.path.getctime(file_path), tz=pytz.UTC
                    )
                    all_files.append((file_path, created_time))

        all_files.sort(key=lambda x: x[1])  # Sort by creation time

        click.echo(f"Total files found: {len(all_files)}")
        if all_files:
            click.echo(f"Oldest file: {all_files[0][0]} (created: {all_files[0][1]})")
            click.echo(f"Newest file: {all_files[-1][0]} (created: {all_files[-1][1]})")

        if from_date:
            all_files = [
                (f, d) for f, d in all_files if d >= from_date.replace(tzinfo=pytz.utc)
            ]
            click.echo(f"Files after {from_date}: {len(all_files)}")

        if from_index is not None:
            all_files = all_files[from_index:]
            click.echo(f"Files from index {from_index}: {len(all_files)}")

        if limit is not None:
            all_files = all_files[:limit]
            click.echo(f"Files limited to: {len(all_files)}")

        total_files = len(all_files)
        tasks = []
        for file_path, _ in all_files:
            tasks.append(process_file(file_path, dest_dir, conn, semaphore, check_mode))

        await asyncio.gather(*tasks)

    # Connect to the SQLite database
    db_path = os.path.join(os.getcwd(), DB_NAME)
    conn = sqlite3.connect(db_path)

    # Destination directory for backups
    dest_dir = Path(os.getcwd())

    # Start processing the files
    click.echo(
        f"Starting backup from {source} to {dest_dir} with concurrency level: {concurrency}"
    )
    asyncio.run(
        process_directory(
            Path(source), dest_dir, conn, concurrency, from_index, from_date, limit
        )
    )
    click.echo(f"Media files backed up from {source} to {dest_dir}")
    click.echo(
        f"Total files: {total_files}, Processed files: {processed_files}, Transferred bytes: {transferred_bytes}"
    )


if __name__ == "__main__":
    mvu()
