"""The account scripts run on the server: list, set a password, delete."""

import sys

import pytest
from sqlalchemy import func, select

import delete_files
import delete_user
import list_users
import set_password
from app import storage
from app.models.audit import AuditEntry
from app.models.file import DONE, File
from app.models.user import MEMBER, User
from app.models.user_session import UserSession
from app.services import audit, auth
from conftest import PASSWORD


@pytest.fixture
def run(monkeypatch):
    """Runs a script's `main()` with these arguments and these answers to its
    prompts (getpass and input alike)."""

    def _run(script, *argv, answers=()):
        replies = iter(answers)
        monkeypatch.setattr(sys, "argv", [script.__name__, *argv])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(replies))
        monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
        return script.main()

    return _run


def _sessions(db, user):
    return db.scalar(select(func.count()).where(UserSession.user_id == user.id))


def test_list_users_shows_every_account(run, make_user, org, admin, capsys):
    member = make_user(org, role=MEMBER)

    assert run(list_users) == 0

    out = capsys.readouterr().out
    assert admin.email in out
    assert member.email in out
    assert org.name in out


def test_set_password_changes_it_and_logs_out_everywhere(run, db, admin):
    auth.open_session(db, admin)
    new = "a brand new password"

    assert run(set_password, admin.email, answers=[new, new]) == 0

    db.expire_all()
    stored = db.get(User, admin.id).password_hash
    assert auth.verify_password(stored, new)
    assert not auth.verify_password(stored, PASSWORD)
    assert _sessions(db, admin) == 0
    assert db.scalar(select(func.count()).where(AuditEntry.event == audit.PASSWORD_SET)) == 1


def test_set_password_refuses_a_short_one(run, db, admin):
    assert run(set_password, admin.email, answers=["short"]) == 1

    db.expire_all()
    assert auth.verify_password(db.get(User, admin.id).password_hash, PASSWORD)


def test_delete_user_keeps_its_files_and_the_log(run, db, org, admin, make_user):
    colleague = make_user(org, role=MEMBER)
    auth.open_session(db, colleague)
    file = File(
        org_id=org.id,
        uploaded_by=colleague.id,
        filename="kept.pdf",
        stored_path="kept.pdf",
        size_bytes=1,
        status=DONE,
    )
    db.add(file)
    db.commit()
    colleague_id, email = colleague.id, colleague.email

    assert run(delete_user, email, answers=[email]) == 0

    db.expire_all()
    assert db.get(User, colleague_id) is None
    assert db.scalar(select(func.count()).where(UserSession.user_id == colleague_id)) == 0
    kept = db.get(File, file.id)
    assert kept is not None
    assert kept.uploaded_by is None
    deleted = db.scalars(select(AuditEntry).where(AuditEntry.event == audit.ACCOUNT_DELETED)).one()
    assert deleted.email == email


def test_delete_user_needs_the_email_typed_again(run, db, org, admin, make_user):
    colleague = make_user(org, role=MEMBER)

    assert run(delete_user, colleague.email, answers=["someone-else@x.test"]) == 1

    db.expire_all()
    assert db.get(User, colleague.id) is not None


def test_delete_user_keeps_the_last_admin(run, db, admin):
    assert run(delete_user, admin.email) == 1

    db.expire_all()
    assert db.get(User, admin.id) is not None


def _file_of(db, org, user, name):
    storage.root().mkdir(parents=True, exist_ok=True)
    storage.path_of(name).write_bytes(b"%PDF-1.4")
    file = File(
        org_id=org.id,
        uploaded_by=user.id if user else None,
        filename=name,
        stored_path=name,
        size_bytes=8,
        status=DONE,
    )
    db.add(file)
    db.commit()
    return file


def test_delete_files_removes_only_that_accounts_rows_and_pdfs(run, db, org, admin, make_user):
    loadtest = make_user(org, role=MEMBER)
    gone = [_file_of(db, org, loadtest, f"loadtest-{n}.pdf") for n in range(3)]
    kept = _file_of(db, org, admin, "admins-own.pdf")
    gone_paths = [storage.path_of(f.stored_path) for f in gone]

    assert run(delete_files, "--uploaded-by", loadtest.email, answers=[loadtest.email]) == 0

    db.expire_all()
    assert db.scalars(select(File.id)).all() == [kept.id]
    assert not any(path.exists() for path in gone_paths)
    assert storage.path_of(kept.stored_path).exists()
    entry = db.scalars(select(AuditEntry).where(AuditEntry.event == audit.FILES_DELETED)).one()
    assert entry.email == loadtest.email


def test_delete_files_needs_the_email_typed_again(run, db, org, make_user):
    loadtest = make_user(org, role=MEMBER)
    file = _file_of(db, org, loadtest, "loadtest-kept.pdf")

    assert run(delete_files, "--uploaded-by", loadtest.email, answers=["no"]) == 1

    db.expire_all()
    assert db.get(File, file.id) is not None
    assert storage.path_of(file.stored_path).exists()
