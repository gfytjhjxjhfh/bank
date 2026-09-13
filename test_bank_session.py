import io
import json
import unittest

import game


class FakeHandler(game.Handler):
    def __init__(self, path, headers=None, body=None):
        self.path = path
        self.headers = dict(headers or {"Content-Length": "0", "Cookie": ""})
        self.rfile = io.BytesIO((body or "").encode())
        self.wfile = io.BytesIO()
        self.headers_out = []
        if body and not self.headers.get("Cookie"):
            try:
                account = game.find_user(json.loads(body).get("name", ""))
            except (TypeError, json.JSONDecodeError):
                account = None
            if account:
                self.headers["Cookie"] = f"bank_session={game.create_session(account)}"

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_out.append((key, value))

    def end_headers(self):
        pass


class SessionFlowTests(unittest.TestCase):
    def test_login_identifier_accepts_name_or_email(self):
        original_users = list(game.users)
        try:
            account = game.User("Alice", "Jones", "secret123", email="alice@example.com")
            game.users[:] = [account]
            self.assertIs(game.find_user_login("Alice Jones"), account)
            self.assertIs(game.find_user_login("alice@example.com"), account)
        finally:
            game.users[:] = original_users

    def test_profile_settings_change_public_identity_only(self):
        original_users = list(game.users)
        try:
            account = game.User("Alice", "Jones", "secret123", email="alice@example.com")
            game.users[:] = [account]
            body = json.dumps({
                "name": account.full_name(),
                "display_name": "BlueFox",
                "profile_icon": "◆",
                "profile_image": "data:image/png;base64,AAAA",
                "theme": "blue",
            })
            handler = FakeHandler(
                "/api/profile/settings",
                headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                body=body,
            )
            handler.do_POST()
            self.assertEqual(account.full_name(), "Alice Jones")
            self.assertEqual(account.profile_username, "BlueFox")
            self.assertEqual(account.profile_icon, "◆")
            self.assertEqual(account.profile_image, "data:image/png;base64,AAAA")
            self.assertEqual(account.theme, "blue")
            self.assertEqual(json.loads(handler.wfile.getvalue())["display_name"], "BlueFox")
            self.assertNotIn("telephone", json.loads(handler.wfile.getvalue()))
            privacy_body = json.dumps({
                "name": account.full_name(), "display_name": "BlueFox",
                "profile_icon": "◆", "theme": "blue", "country": "Poland",
                "show_country": True, "telephone": "+48 123 456 789",
            })
            handler = FakeHandler(
                "/api/profile/settings",
                headers={"Content-Length": str(len(privacy_body.encode())), "Cookie": ""},
                body=privacy_body,
            )
            handler.do_POST()
            self.assertEqual(account.telephone, "+48 123 456 789")
            self.assertEqual(account.country, "Poland")
            self.assertTrue(account.show_country)
            self.assertEqual(json.loads(handler.wfile.getvalue())["country"], "Poland")
            other = game.User("Bob", "Smith", "secret123", email="bob@example.com")
            game.users.append(other)
            duplicate = json.dumps({
                "name": other.full_name(), "display_name": "BlueFox",
                "profile_icon": "●", "theme": "green",
            })
            handler = FakeHandler(
                "/api/profile/settings",
                headers={"Content-Length": str(len(duplicate.encode())), "Cookie": ""},
                body=duplicate,
            )
            handler.do_POST()
            self.assertEqual(handler.status, 409)
        finally:
            game.users[:] = original_users

    def test_search_and_add_friend_by_public_username(self):
        original_users = list(game.users)
        try:
            alice = game.User("Alice", "Jones", "secret123", email="alice@example.com",
                              profile_username="BlueFox")
            bob = game.User("Bob", "Smith", "secret123", email="bob@example.com",
                            profile_username="GreenBear")
            game.users[:] = [alice, bob]
            body = json.dumps({"name": alice.full_name(), "query": "GreenBear"})
            handler = FakeHandler(
                "/api/friends/search",
                headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                body=body,
            )
            handler.do_POST()
            results = json.loads(handler.wfile.getvalue())["results"]
            self.assertEqual(results[0]["display_name"], "GreenBear")
            self.assertEqual(results[0]["email"], bob.email)

            body = json.dumps({"name": alice.full_name(), "email": bob.email})
            handler = FakeHandler(
                "/api/friends/add",
                headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                body=body,
            )
            handler.do_POST()
            self.assertIn(bob.email, alice.friend_emails)
            self.assertIn(alice.email, bob.friend_emails)
        finally:
            game.users[:] = original_users

    def test_friend_messages_support_text_and_reject_oversized_media(self):
        original_users = list(game.users)
        original_messages = list(game.messages)
        try:
            alice = game.User("Alice", "Jones", "secret123", email="alice@example.com")
            bob = game.User("Bob", "Smith", "secret123", email="bob@example.com")
            alice.friend_emails = [bob.email]
            bob.friend_emails = [alice.email]
            game.users[:] = [alice, bob]
            game.messages[:] = []

            def post(payload):
                body = json.dumps(payload)
                handler = FakeHandler(
                    "/api/messages",
                    headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                    body=body,
                )
                handler.do_POST()
                return handler

            handler = post({"name": alice.full_name(), "friend_email": bob.email,
                            "type": "emoji", "content": "😀"})
            self.assertEqual(handler.status, 200)
            self.assertEqual(game.messages[0]["content"], "😀")
            original_limit = game.MAX_MESSAGE_FILE_SIZE
            game.MAX_MESSAGE_FILE_SIZE = 10
            rejected = post({"name": alice.full_name(), "friend_email": bob.email,
                              "type": "photo", "content": "data:image/png;base64," + "A" * 20})
            game.MAX_MESSAGE_FILE_SIZE = original_limit
            self.assertEqual(rejected.status, 413)
        finally:
            game.users[:] = original_users
            game.messages[:] = original_messages
            game.save_messages(game.messages)

    def test_group_chat_is_only_available_to_members(self):
        original_users = list(game.users)
        original_groups = list(game.groups)
        original_messages = list(game.messages)
        try:
            creator = game.User("Alice", "Jones", "secret123", email="alice@example.com")
            outsider = game.User("Bob", "Smith", "secret123", email="bob@example.com")
            game.users[:] = [creator, outsider]
            game.groups[:] = [{"id": "group-1", "name": "Travel", "balance": 0,
                               "creator_email": creator.email,
                               "members": [{"email": creator.email, "name": creator.full_name(),
                                            "role": "creator"}],
                               "join_requests": [], "history": []}]
            game.messages[:] = []

            def post(payload):
                body = json.dumps(payload)
                handler = FakeHandler(
                    "/api/messages",
                    headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                    body=body,
                )
                handler.do_POST()
                return handler

            sent = post({"name": creator.full_name(), "group_id": "group-1",
                         "type": "text", "content": "Hello group"})
            self.assertEqual(sent.status, 200)
            denied = post({"name": outsider.full_name(), "group_id": "group-1",
                           "type": "text", "content": "Not allowed"})
            self.assertEqual(denied.status, 403)
        finally:
            game.users[:] = original_users
            game.groups[:] = original_groups
            game.messages[:] = original_messages
            game.save_messages(game.messages)

    def test_only_group_creator_can_delete_group(self):
        original_users = list(game.users)
        original_groups = list(game.groups)
        original_messages = list(game.messages)
        try:
            creator = game.User("Alice", "Jones", "secret123", email="alice@example.com")
            member = game.User("Bob", "Smith", "secret123", email="bob@example.com")
            member.auto_deposit_group_id = "group-1"
            game.users[:] = [creator, member]
            game.groups[:] = [{"id": "group-1", "name": "Travel", "balance": 10,
                               "creator_email": creator.email,
                               "members": [{"email": creator.email, "name": creator.full_name(),
                                            "role": "creator"},
                                           {"email": member.email, "name": member.full_name(),
                                            "role": "member"}],
                               "join_requests": [], "history": []}]
            game.messages[:] = [{"group_id": "group-1", "content": "hello"}]

            def post(name):
                body = json.dumps({"name": name, "group_id": "group-1"})
                handler = FakeHandler(
                    "/api/groups/delete",
                    headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                    body=body,
                )
                handler.do_POST()
                return handler

            denied = post(member.full_name())
            self.assertEqual(denied.status, 403)
            self.assertEqual(len(game.groups), 1)

            deleted = post(creator.full_name())
            self.assertEqual(deleted.status, 200)
            self.assertEqual(game.groups, [])
            self.assertEqual(member.auto_deposit_group_id, "")
            self.assertEqual(game.messages, [])
        finally:
            game.users[:] = original_users
            game.groups[:] = original_groups
            game.messages[:] = original_messages
            game.save_messages(game.messages)

    def test_register_sets_cookie(self):
        original_users = list(game.users)
        try:
            game.users[:] = []
            handler = FakeHandler(
                "/api/register",
                headers={"Content-Length": str(len(json.dumps({
                    "first_name": "Test",
                    "last_name": "User",
                    "email": "test@example.com",
                    "password": "secret123",
                    "accept_terms": "yes",
                }).encode())), "Cookie": ""},
                body=json.dumps({
                    "first_name": "Test",
                    "last_name": "User",
                    "email": "test@example.com",
                    "password": "secret123",
                    "accept_terms": "yes",
                })
            )
            handler.do_POST()
            cookie_header = next((value for key, value in handler.headers_out if key == "Set-Cookie"), "")
            self.assertIn("bank_session=", cookie_header)
            self.assertIn("HttpOnly", cookie_header)
            self.assertTrue(game.users[0].terms_accepted_at)
        finally:
            game.users[:] = original_users
            game.save_users(game.users)

    def test_register_requires_rules_and_data_consent(self):
        original_users = list(game.users)
        try:
            game.users[:] = []
            body = json.dumps({
                "first_name": "No",
                "last_name": "Consent",
                "email": "no-consent@example.com",
                "password": "secret123",
            })
            handler = FakeHandler(
                "/api/register",
                headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                body=body,
            )
            handler.do_POST()
            self.assertEqual(handler.status, 400)
            self.assertEqual(game.users, [])
        finally:
            game.users[:] = original_users

    def test_protected_action_requires_server_session(self):
        original_users = list(game.users)
        try:
            account = game.User("Alice", "Jones", "secret123", balance=20,
                                email="alice@example.com")
            game.users[:] = [account]
            body = json.dumps({"name": account.full_name(), "type": "withdraw",
                               "amount": 5})
            handler = FakeHandler(
                "/api/transaction",
                headers={"Content-Length": str(len(body.encode())),
                         "Cookie": "bank_session=invalid"},
                body=body,
            )
            handler.do_POST()
            self.assertEqual(handler.status, 401)
            self.assertEqual(account.balance, 20)
        finally:
            game.users[:] = original_users

    def test_callback_uses_bank_cookie_when_state_is_missing(self):
        original_users = list(game.users)
        try:
            game.users[:] = [game.User("Alice", "Jones", "secret123", email="alice@example.com")]
            game.oauth_states.clear()
            handler = FakeHandler(
                "/auth/discord/callback?code=test-code",
                headers={"Cookie": "bank_user=Alice%20Jones"},
            )
            handler.do_GET()
            self.assertNotEqual(getattr(handler, "status", 200), 400)
            self.assertNotIn("requires a logged-in bank account", handler.wfile.getvalue().decode("utf-8", "ignore").lower())
        finally:
            game.users[:] = original_users
            game.save_users(game.users)

    def test_transfer_request_is_accepted_by_recipient(self):
        original_users = list(game.users)
        try:
            sender = game.User("Alice", "Jones", "secret123", balance=75,
                               email="alice@example.com")
            recipient = game.User("Bob", "Smith", "secret123", balance=10,
                                  email="bob@example.com")
            game.users[:] = [sender, recipient]
            request_body = json.dumps({
                "name": sender.full_name(),
                "email": recipient.email,
                "amount": 25,
            })
            handler = FakeHandler(
                "/api/transfer/request",
                headers={"Content-Length": str(len(request_body.encode())), "Cookie": ""},
                body=request_body,
            )
            handler.do_POST()
            request = sender.transfer_requests[0]
            self.assertEqual(sender.balance, 75)
            self.assertEqual(recipient.balance, 10)

            accept_body = json.dumps({
                "name": recipient.full_name(),
                "id": request["id"],
                "action": "accept",
            })
            handler = FakeHandler(
                "/api/transfer/respond",
                headers={"Content-Length": str(len(accept_body.encode())), "Cookie": ""},
                body=accept_body,
            )
            handler.do_POST()
            self.assertEqual(sender.balance, 50)
            self.assertEqual(recipient.balance, 35)
            self.assertEqual(sender.transfer_requests[0]["status"], "accepted")
            self.assertEqual(recipient.transfer_requests[0]["status"], "accepted")
        finally:
            game.users[:] = original_users
            game.save_users(game.users)

    def test_group_permissions_and_money_flow(self):
        original_users = list(game.users)
        original_groups = list(game.groups)
        try:
            creator = game.User("Alice", "Jones", "secret123", balance=100,
                                email="alice@example.com")
            member = game.User("Bob", "Smith", "secret123", balance=50,
                               email="bob@example.com")
            game.users[:] = [creator, member]
            game.groups[:] = []

            def post(path, payload):
                body = json.dumps(payload)
                handler = FakeHandler(
                    path,
                    headers={"Content-Length": str(len(body.encode())), "Cookie": ""},
                    body=body,
                )
                handler.do_POST()
                return handler

            post("/api/groups/create", {"name": creator.full_name(), "group_name": "Travel fund"})
            group = game.groups[0]
            image_body = json.dumps({
                "name": creator.full_name(), "group_id": group["id"],
                "group_image": "data:image/png;base64,AAAA",
            })
            image_handler = FakeHandler(
                "/api/groups/profile",
                headers={"Content-Length": str(len(image_body.encode())), "Cookie": ""},
                body=image_body,
            )
            image_handler.do_POST()
            self.assertEqual(group["group_image"], "data:image/png;base64,AAAA")
            post("/api/groups/join", {"name": member.full_name(), "group_id": group["id"]})
            post("/api/groups/respond", {
                "name": creator.full_name(), "group_id": group["id"],
                "email": member.email, "action": "accept",
            })
            post("/api/groups/deposit", {
                "name": member.full_name(), "group_id": group["id"], "amount": 20,
            })
            self.assertEqual(group["balance"], 20)
            self.assertEqual(member.balance, 30)
            self.assertTrue(any("deposited" in item["message"]
                                for item in creator.notifications))

            denied = post("/api/groups/withdraw", {
                "name": member.full_name(), "group_id": group["id"], "amount": 1,
            })
            self.assertEqual(denied.status, 403)
            post("/api/groups/role", {
                "name": creator.full_name(), "group_id": group["id"],
                "email": member.email, "role": "subcreator",
            })
            post("/api/groups/withdraw", {
                "name": member.full_name(), "group_id": group["id"], "amount": 5,
            })
            self.assertEqual(group["balance"], 15)
            self.assertEqual(member.balance, 35)
            self.assertEqual(game.group_member(group, member.email)["role"], "subcreator")

            post("/api/profile/auto-deposit", {
                "name": creator.full_name(), "group_id": group["id"],
            })
            post("/api/transaction", {
                "name": creator.full_name(), "type": "deposit", "amount": 10,
            })
            self.assertEqual(group["balance"], 25)
            self.assertEqual(creator.balance, 100)

            denied_kick = post("/api/groups/kick", {
                "name": member.full_name(), "group_id": group["id"],
                "email": creator.email,
            })
            self.assertEqual(denied_kick.status, 403)
            notification_id = creator.notifications[0]["id"]
            read = post("/api/notifications/read", {
                "name": creator.full_name(), "id": notification_id,
            })
            self.assertEqual(read.status, 200)
            self.assertTrue(all(item["read"] for item in creator.notifications
                                if item["id"] == notification_id))
        finally:
            game.users[:] = original_users
            game.groups[:] = original_groups
            game.save_users(game.users)
            game.save_groups(game.groups)


if __name__ == "__main__":
    unittest.main()
