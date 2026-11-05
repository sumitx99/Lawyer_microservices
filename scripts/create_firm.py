"""Create a firm and its first Admin console user (run by Lexaior staff at onboarding).

    python -m scripts.create_firm --firm "Smith Injury Law" --slug smith --email admin@smithlaw.com --name "Dana Smith"

AUTH_PROVIDER=workos: creates a WorkOS Organization for the firm and emails the admin a
WorkOS invitation. They accept it, set a password (and MFA), and land in the Admin console.
AUTH_PROVIDER=dev: prints a temporary password instead."""
import argparse
import asyncio

from sqlalchemy import select

from pymicroservices.app.utilities.firm_db import SessionLocal
from pymicroservices.app.firm_models import Admin, Firm
from pymicroservices.app.security.permissions import sync_permission_sets
from pymicroservices.app.security.providers import WorkOSProvider, get_provider


async def main(args) -> None:
    with SessionLocal() as db:
        sync_permission_sets(db)
        if db.scalar(select(Firm).where(Firm.slug == args.slug)):
            raise SystemExit(f"Firm slug '{args.slug}' already exists")
        email = args.email.lower()
        if db.scalar(select(Admin).where(Admin.email == email)):
            raise SystemExit(f"{email} is already an admin")
        provider = get_provider("admin")
        firm = Firm(name=args.firm, slug=args.slug, require_mfa=not args.no_mfa)
        if isinstance(provider, WorkOSProvider):
            firm.workos_organization_id = await provider.create_organization(args.firm)
        db.add(firm)
        db.flush()
        subject, temp = await provider.create_user(db, email, args.name, organization_id=firm.workos_organization_id)
        db.add(Admin(firm_id=firm.id, email=email, name=args.name, provider_subject=subject, status="invited"))
        db.commit()
        print(f"Created firm {firm.name} ({firm.id}) with admin {email}")
        if firm.workos_organization_id:
            print(f"WorkOS organization {firm.workos_organization_id}; invitation emailed to {email}")
        if temp:
            print(f"Temporary password (dev provider): {temp}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--firm", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--no-mfa", action="store_true", help="don't require MFA for IAM users")
    asyncio.run(main(p.parse_args()))
