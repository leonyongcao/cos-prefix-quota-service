import argparse

from app.database import Base, SessionLocal, engine
from app.quota_service import QuotaService
from app.schemas import normalize_prefix

Base.metadata.create_all(bind=engine)


def main():
    parser = argparse.ArgumentParser(description="Reconcile COS prefix usage with quota database")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--apply", action="store_true", help="write actual usage back to DB")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = QuotaService().reconcile_prefix(db, args.bucket, normalize_prefix(args.prefix), dry_run=not args.apply)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
