"""Loads the nine sample documents from sample_data/ into an explicitly selected database.
Usage: python sample_seed.py --confirm-destructive
Reads MONGO_URI / MONGO_DB from the environment or .env.

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show.
Passing on this sample does not mean your analytics are correct - re-read the business rules.
The script deletes existing employees and attendance_logs before inserting the samples."""
import argparse
import os
import pathlib
from typing import Any

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

opts = json_util.JSONOptions(tz_aware=True)


def load_sample_documents(sample_directory: pathlib.Path) -> dict[str, list[dict[str, Any]]]:
    collections: dict[str, list[dict[str, Any]]] = {}
    for name in ("employees", "attendance_logs"):
        source = sample_directory / f"{name}.json"
        documents = json_util.loads(source.read_text(encoding="utf-8"), json_options=opts)
        if not isinstance(documents, list) or not all(isinstance(doc, dict) for doc in documents):
            raise ValueError(f"{source} must contain a JSON array of documents")
        collections[name] = documents
    return collections


def seed_database(database: Any, sample_directory: pathlib.Path) -> None:
    collections = load_sample_documents(sample_directory)
    database.command("ping")
    for name, documents in collections.items():
        database[name].delete_many({})
        if documents:
            database[name].insert_many(documents)
        print(f"loaded {len(documents):>2} documents into '{database.name}.{name}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replace attendance collections with supplied sample data.")
    parser.add_argument(
        "--confirm-destructive",
        action="store_true",
        help="confirm that the configured employees and attendance_logs collections will be deleted",
    )
    args = parser.parse_args()
    if not args.confirm_destructive:
        parser.error("refusing to delete collections without --confirm-destructive")

    load_dotenv()
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
    database = client[os.getenv("MONGO_DB", "attendance_db")]
    try:
        seed_database(database, pathlib.Path(__file__).parent / "sample_data")
    finally:
        client.close()


if __name__ == "__main__":
    main()
