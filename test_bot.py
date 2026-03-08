"""
Тесты для бота — только стандартная библиотека Python (unittest + sqlite3).
Запуск: python3 test_bot.py
"""
import os, sys, sqlite3, tempfile, unittest

sys.path.insert(0, os.path.dirname(__file__))
from bot import extract_client_data, find_client, save_client, init_db, send_private_message


def make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_db(tmp.name)
    return tmp.name


class TestExtractClientData(unittest.TestCase):
    MSG = ("#ФРАУ_КУХНИ Игорь зв 06.03 +79135681086\n"
           "3. Бюджет до 700 000 р\n"
           "5. установка город Красноярск")

    def test_phone(self):
        phone, *_ = extract_client_data(self.MSG)
        self.assertEqual(phone, "+79135681086")

    def test_region(self):
        _, _, region, _, _ = extract_client_data(self.MSG)
        self.assertEqual(region, "Красноярск")

    def test_budget(self):
        _, _, _, budget, _ = extract_client_data(self.MSG)
        self.assertIn("700 000", budget)

    def test_tag(self):
        _, _, _, _, tags = extract_client_data(self.MSG)
        self.assertIn("#ФРАУ_КУХНИ", tags)

    def test_username(self):
        msg = "#ФРАУ_КУХНИ @ivanov город Москва"
        _, username, _, _, _ = extract_client_data(msg)
        self.assertEqual(username, "@ivanov")

    def test_no_phone(self):
        phone, *_ = extract_client_data("#ФРАУ_КУХНИ текст")
        self.assertIsNone(phone)

    def test_empty(self):
        self.assertEqual(extract_client_data(""), (None, None, None, None, []))

    def test_none(self):
        self.assertEqual(extract_client_data(None), (None, None, None, None, []))

    def test_multiple_tags(self):
        msg = "#ФРАУ_КУХНИ #VIP #МОСКВА +79991234567"
        _, _, _, _, tags = extract_client_data(msg)
        self.assertIn("#VIP", tags)

    def test_ukrainian_phone(self):
        phone, *_ = extract_client_data("#ФРАУ_КУХНИ +380671234567")
        self.assertEqual(phone, "+380671234567")

    def test_us_phone(self):
        phone, *_ = extract_client_data("#ФРАУ_КУХНИ +12025551234")
        self.assertEqual(phone, "+12025551234")


class TestDatabase(unittest.TestCase):
    def setUp(self): self.db = make_db()
    def tearDown(self): os.unlink(self.db)

    def test_find_by_phone(self):
        save_client("+79135681086", None, "Красноярск", "700k", ["#ФРАУ_КУХНИ"], self.db)
        self.assertIsNotNone(find_client("+79135681086", None, self.db))

    def test_find_by_username(self):
        save_client(None, "@petrov", "Омск", "200k", [], self.db)
        self.assertIsNotNone(find_client(None, "@petrov", self.db))

    def test_not_found(self):
        self.assertIsNone(find_client("+70000000000", "@nobody", self.db))

    def test_no_criteria(self):
        self.assertIsNone(find_client(None, None, self.db))

    def test_save_returns_id(self):
        nid = save_client("+79991234567", None, "Тюмень", "150k", [], self.db)
        self.assertIsInstance(nid, int)
        self.assertGreater(nid, 0)

    def test_distinct_ids(self):
        id1 = save_client("+79991111111", "@alpha", "Екб", "200k", [], self.db)
        id2 = save_client("+79992222222", "@beta",  "Чел", "400k", [], self.db)
        self.assertNotEqual(id1, id2)

    def test_fields_stored(self):
        save_client("+79876543210", "@anna", "Новосибирск", "до 450к",
                    ["#ФРАУ_КУХНИ"], self.db)
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT phone,username,region,budget,tags FROM clients").fetchone()
        self.assertEqual(row[0], "+79876543210")
        self.assertEqual(row[1], "@anna")
        self.assertEqual(row[2], "Новосибирск")
        self.assertIn("#ФРАУ_КУХНИ", row[4])


class TestSendPrivateMessage(unittest.TestCase):
    class BotOK:
        def send_message(self, chat_id, text): pass
    class BotFail:
        def send_message(self, chat_id, text): raise Exception("fail")

    def test_success(self):
        self.assertTrue(send_private_message(self.BotOK(), 1, "ok"))

    def test_failure(self):
        self.assertFalse(send_private_message(self.BotFail(), 1, "ok"))

    def test_no_uncaught_exception(self):
        try:
            send_private_message(self.BotFail(), 1, "x")
        except Exception:
            self.fail("send_private_message пробросила исключение")


class TestIntegration(unittest.TestCase):
    def setUp(self): self.db = make_db()
    def tearDown(self): os.unlink(self.db)

    def test_new_client_saved(self):
        msg = ("#ФРАУ_КУХНИ Анна +79876543210\n"
               "Бюджет до 450 000 р\n"
               "город Новосибирск")
        phone, username, region, budget, tags = extract_client_data(msg)
        self.assertIsNone(find_client(phone, username, self.db))
        save_client(phone, username, region, budget, tags, self.db)
        self.assertIsNotNone(find_client(phone, username, self.db))

    def test_existing_client_gets_message(self):
        sent = []
        class CapBot:
            def send_message(self, chat_id, text): sent.append(chat_id)

        cid = save_client("+79991110000", None, "Москва", "1M", [], self.db)
        found = find_client("+79991110000", None, self.db)
        if found:
            send_private_message(CapBot(), found, "привет")
        self.assertIn(cid, sent)


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromNames([
        "test_bot.TestExtractClientData",
        "test_bot.TestDatabase",
        "test_bot.TestSendPrivateMessage",
        "test_bot.TestIntegration",
    ], module=sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"\n{'='*55}")
    print(f"  ИТОГ: {passed}/{result.testsRun} тестов прошли")
    print("  ✅ ВСЕ ТЕСТЫ ПРОШЛИ" if not (result.failures or result.errors) else "  ❌ ЕСТЬ ПРОВАЛЫ")
    print(f"{'='*55}")
    sys.exit(len(result.failures) + len(result.errors))
