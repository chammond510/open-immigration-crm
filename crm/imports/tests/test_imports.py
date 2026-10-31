import csv
import io
from datetime import date, timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from crm.connect.scheduler import tick
from crm.customfields.models import CustomField
from crm.imports import spreadsheet
from crm.imports.models import SpreadsheetImport
from crm.imports.spreadsheet import SpreadsheetError, read_csv, suggest_mapping
from crm.models import Activity, AuditLog, Contact, Matter
from crm.tests.factories import make_contact, make_matter, make_user

HEADERS = "First Name,Last Name,Email,Mobile,DOB,Matter,Case type,Receipt #,Attorney\n"


class ReadingTests(SimpleTestCase):
    def test_files_from_spreadsheet_apps(self):
        cases = {
            "utf-8 with a byte-order mark": "﻿Name,Email\nRenée Fictional,r@example.test\n".encode(),
            "windows encoding": "Name,Email\nRenée Fictional,r@example.test\n".encode("cp1252"),
            "semicolons": b"Name;Email\nRen\xc3\xa9e Fictional;r@example.test\n",
        }
        for case, data in cases.items():
            with self.subTest(case=case):
                _, headers, rows = read_csv(data)
                self.assertEqual(headers, ["Name", "Email"])
                self.assertEqual(rows, [["Renée Fictional", "r@example.test"]])
        _, _, rows = read_csv(b'Name,Notes,Extra\n"Lena Fictional","Line one\nLine two"\n')
        self.assertEqual(rows, [["Lena Fictional", "Line one\nLine two", ""]])

    def test_files_it_refuses(self):
        cases = {
            b"PK\x03\x04\x00\x00binary": "not a CSV file",
            b"\n\n": "header row",
            b"Name,Email\n": "no rows",
            b"Name\n" + b"Someone\n" * 4: "more than 3 rows",
            b"x" * 50: "larger than",
        }
        with (
            mock.patch.object(spreadsheet, "MAX_ROWS", 3),
            mock.patch.object(spreadsheet, "MAX_BYTES", 40),
        ):
            for data, message in cases.items():
                with (
                    self.subTest(message=message),
                    self.assertRaisesMessage(SpreadsheetError, message),
                ):
                    read_csv(data)
        with self.assertRaisesMessage(SpreadsheetError, "could not be read"):
            read_csv(b"Name\n\x81\x8d\n")
        with self.assertRaisesMessage(SpreadsheetError, "header row"):
            read_csv(b"")
        # A cell larger than Python's CSV reader allows, such as a pasted document.
        with self.assertRaisesMessage(SpreadsheetError, "not valid CSV"):
            read_csv(b'Name\n"' + b"x" * 200_000 + b'"\n')


class MappingTests(TestCase):
    def test_headers_are_matched_to_fields_once(self):
        CustomField.objects.create(
            applies_to="contact", label="Lead source", key="lead_source", field_type="text"
        )
        headers = ["Client Name", "E-mail", "Cell", "Name", "Receipt #", "Lead Source", "Shoe"]
        self.assertEqual(
            suggest_mapping(headers),
            {
                "0": "full_name",
                "1": "email",
                "2": "phone",
                "4": "receipt_number",
                "5": "contact:lead_source",
            },
        )
        self.assertIn("Lead source", spreadsheet.template_headers())
        self.assertNotIn("Full name", spreadsheet.template_headers())


class ImportCase(TestCase):
    def setUp(self):
        self.admin = make_user()
        self.client.force_login(self.admin)

    def upload(self, text, name="clients.csv"):
        answer = self.client.post(
            reverse("import_list"), {"file": SimpleUploadedFile(name, text.encode())}
        )
        item = SpreadsheetImport.objects.first()
        self.assertRedirects(answer, reverse("import_columns", args=[item.pk]))
        return item

    def match(self, item, columns=None, **options):
        data = {
            "date_order": "mdy",
            "contact_status": "client",
            "stage": "active",
            "matter_status": "open",
            "assigned_to": item.options.get("assigned_to") or "",
            **options,
        }
        mapping = columns if columns is not None else item.mapping
        for index in range(len(item.headers)):
            data[f"column_{index}"] = mapping.get(str(index), "")
        return self.client.post(reverse("import_columns", args=[item.pk]), data)

    def prepare(self, rows, header=HEADERS, **options):
        item = self.upload(header + rows)
        answer = self.match(item, **options)
        self.assertRedirects(answer, reverse("import_preview", args=[item.pk]))
        item.refresh_from_db()
        return item

    def confirm(self, item):
        answer = self.client.post(reverse("import_preview", args=[item.pk]))
        self.assertRedirects(answer, reverse("import_report", args=[item.pk]))
        item.refresh_from_db()
        return item

    def reasons(self, item):
        return {entry["row"]: entry["reason"] for entry in item.skipped}


class ImportFlowTests(ImportCase):
    def test_upload_match_preview_and_import(self):
        make_user("paralegal", superuser=False)
        item = self.upload(
            HEADERS + "Lena,Fictional,lena@example.test,(202) 555-0144,03/31/1990,"
            "Spousal petition,Family-based,IOE-0900-000-001,paralegal@example.test\n"
            ",,,,,,,,\n"
            "Omar,Fictional,omar@example.test,202-555-0145,,,,,\n"
        )
        self.assertEqual((item.row_count, item.filename), (3, "clients.csv"))
        self.assertEqual(item.options, {"assigned_to": self.admin.pk})
        page = self.client.get(reverse("import_columns", args=[item.pk]))
        self.assertContains(page, "lena@example.test")
        self.assertEqual(page.context["form"]["column_6"].value(), "case_type")

        preview = self.client.get(reverse("import_preview", args=[item.pk]))
        self.assertRedirects(preview, reverse("import_columns", args=[item.pk]))
        self.assertRedirects(self.match(item), reverse("import_preview", args=[item.pk]))
        preview = self.client.get(reverse("import_preview", args=[item.pk]))
        self.assertContains(preview, "Import 2 contacts and 1 matter")
        self.assertContains(preview, "Spousal petition")
        self.assertNotContains(preview, "cannot be matched")
        self.assertEqual(Contact.objects.count(), 0)  # Nothing is saved before confirming.

        self.confirm(item)
        lena = Contact.objects.get(email="lena@example.test")
        self.assertEqual(
            (lena.full_name, lena.phone_e164, lena.date_of_birth, lena.status, lena.stage),
            ("Lena Fictional", "+12025550144", date(1990, 3, 31), "client", "active"),
        )
        self.assertEqual(lena.assigned_to.username, "paralegal")
        matter = Matter.objects.get()
        self.assertEqual(
            (matter.primary_contact, matter.case_type, matter.status, matter.assigned_to.username),
            (lena, "family", "open", "paralegal"),
        )
        omar = Contact.objects.get(email="omar@example.test")
        self.assertEqual(omar.assigned_to, self.admin)  # The default for rows without one.
        self.assertEqual(
            sorted(Activity.objects.values_list("subject", flat=True)),
            sorted(
                [
                    "Contact imported from a spreadsheet",
                    "Contact imported from a spreadsheet",
                    f"Matter {matter.matter_number} imported from a spreadsheet",
                ]
            ),
        )
        self.assertEqual(
            (item.status, item.contacts_created, item.matters_created, item.skipped),
            ("imported", 2, 1, []),
        )
        log = AuditLog.objects.get(action="data.imported")
        self.assertIn("2 contacts and 1 matter from", log.detail)
        report = self.client.get(reverse("import_report", args=[item.pk]))
        self.assertContains(report, "Every row was imported.")

        # Confirming again, or going back to the columns, imports nothing more.
        self.client.post(reverse("import_preview", args=[item.pk]))
        self.assertRedirects(
            self.client.get(reverse("import_columns", args=[item.pk])),
            reverse("import_report", args=[item.pk]),
        )
        self.assertEqual(Contact.objects.count(), 2)
        listing = self.client.get(reverse("import_list"))
        self.assertContains(listing, "clients.csv")

    def test_matching_contacts_and_matters_already_known(self):
        known = make_contact(email="amina@example.test", phone="+1 202 555 0150")
        make_matter(known, receipt_number="IOE0900000099")
        make_contact(first_name="Twin", email="twin@example.test")
        make_contact(first_name="Other", email="other@example.test", phone="202-555-0151")
        item = self.prepare(
            "Amina,Example,AMINA@example.test,,,Naturalization,,,\n"  # 2: matter added
            "Amina,Again,,2025550150,,,,,\n"  # 3: same person by phone, no matter
            "Nadia,Fictional,nadia@example.test,,,Asylum,,,\n"  # 4: new
            "Nadia,Fictional,nadia@example.test,,,Waiver,,,\n"  # 5: matter for row 4
            "Nadia,Fictional,,,,,,,\n"  # 6: no email or phone: added again
            "Nadia,F.,nadia@example.test,,,,,,\n"  # 7: same as row 4, no matter
            "Twin,Fictional,twin@example.test,202-555-0151,,Appeal,,,\n"  # 8: two contacts
            "Pat,Fictional,pat@example.test,,,Renewal,,ioe 0900 000 099,\n"  # 9: receipt known
            "Sam,Fictional,sam@example.test,,,Petition,,IOE0900000100,\n"  # 10: new
            "Sam,Fictional,sam@example.test,,,Petition,,IOE0900000100,\n"  # 11: receipt repeated
        )
        self.confirm(item)
        self.assertEqual(
            self.reasons(item),
            {
                3: "Already in the CRM: a contact has the same email or phone.",
                7: "Same email or phone as row 4, and no matter to add.",
                8: "Its email or phone matches more than one contact in the CRM.",
                9: "A matter with this receipt number is already in the CRM or earlier in the "
                "file.",
                11: "A matter with this receipt number is already in the CRM or earlier in the "
                "file.",
            },
        )
        known.refresh_from_db()
        self.assertEqual(known.last_name, "Example")  # Never changed by a row.
        self.assertEqual(
            sorted(known.matters.values_list("title", flat=True)),
            ["Fictional family matter", "Naturalization"],
        )
        nadia = Contact.objects.filter(first_name="Nadia")
        self.assertEqual(nadia.count(), 2)
        self.assertEqual(
            sorted(nadia.get(email="nadia@example.test").matters.values_list("title", flat=True)),
            ["Asylum", "Waiver"],
        )
        self.assertEqual((item.contacts_created, item.matters_created), (3, 4))

    def test_rows_the_forms_refuse_are_reported_without_their_values(self):
        CustomField.objects.create(
            applies_to="contact",
            label="Lead source",
            key="lead_source",
            field_type="choice",
            choices="Google Ads\nReferral",
        )
        CustomField.objects.create(
            applies_to="matter", label="Pro bono", key="pro_bono", field_type="yes_no"
        )
        CustomField.objects.create(
            applies_to="matter", label="Biometrics", key="biometrics", field_type="date"
        )
        header = "Name,Email,Born,Lead source,Status,Matter,Type,Pro bono,Biometrics,Attorney\n"
        item = self.upload(
            header + "Lena Fictional,lena@example.test,31/03/1990,google ads,Prospective client,"
            "Petition,family,yes,01/04/2026,attorney\n"  # 2: imported
            "Bad Email,not-an-email@,,,,,,,,\n"  # 3
            "Madonna,m@example.test,,,,,,,,\n"  # 4: no last name
            "Old Date,old@example.test,31/03/90,,,,,,,\n"  # 5: two-digit year
            "Odd Choice,odd@example.test,,Billboard,,,,,,\n"  # 6
            "Odd Status,status@example.test,,,Famous,,,,,\n"  # 7
            "Odd Answer,answer@example.test,,,,Petition,,maybe,,\n"  # 8
            "No Title,title@example.test,,,,,employment,,,\n"  # 9: matter with no title
            "Unknown Staff,staff@example.test,,,,,,,,someone@example.test\n"  # 10
        )
        self.assertEqual(
            item.mapping,
            {
                "0": "full_name",
                "1": "email",
                "3": "contact:lead_source",
                "5": "matter_title",
                "7": "matter:pro_bono",
                "8": "matter:biometrics",
                "9": "assigned_to",
            },
        )
        columns = {
            **item.mapping,
            "2": "date_of_birth",
            "4": "contact_status",
            "6": "case_type",
        }
        self.assertRedirects(
            self.match(item, columns, date_order="dmy"),
            reverse("import_preview", args=[item.pk]),
        )
        item.refresh_from_db()
        self.confirm(item)
        reasons = self.reasons(item)
        self.assertEqual(
            reasons,
            {
                3: "Email: Enter a valid email address.",
                4: "Last name: This field is required.",
                5: "Date of birth: enter a full date, written as the import's dates are.",
                6: "Lead source: not one of the choices.",
                7: "Contact status: not one of the choices.",
                8: "Pro bono: answer yes or no.",
                9: "Matter title: This field is required.",
                10: "Assigned to: no active staff member has that email or username.",
            },
        )
        for value in ("not-an-email", "Billboard", "Famous", "maybe", "someone@"):
            self.assertNotIn(value, " ".join(reasons.values()))
        lena = Contact.objects.get()
        self.assertEqual(
            (lena.date_of_birth, lena.status, lena.custom_fields),
            (date(1990, 3, 31), "prospect", {"lead_source": "Google Ads"}),
        )
        matter = Matter.objects.get()
        self.assertEqual(
            (matter.case_type, matter.custom_fields, matter.assigned_to),
            ("family", {"pro_bono": True, "biometrics": "2026-04-01"}, self.admin),
        )

    def test_matching_columns_is_checked(self):
        item = self.upload("Name,Matter,Type,Also name\nLena Fictional,Petition,family,x\n")
        cases = [
            ({"0": "full_name", "3": "full_name"}, "Full name is already filled from “Name”"),
            ({"1": "matter_title", "2": "case_type"}, "each person&#x27;s name"),
            ({"0": "full_name", "2": "case_type"}, "each matter&#x27;s title"),
        ]
        for columns, message in cases:
            with self.subTest(message=message):
                self.assertContains(self.match(item, columns), message)

    def test_a_contact_import_warns_when_rows_cannot_be_matched(self):
        item = self.prepare("Lena Fictional\n", header="Name\n")
        preview = self.client.get(reverse("import_preview", args=[item.pk]))
        self.assertContains(preview, "Importing the same file twice would add everyone twice.")
        self.assertContains(preview, "Import 1 contact and 0 matters")


class SkippedRowsAndFilesTests(ImportCase):
    def test_skipped_rows_download_for_fixing(self):
        item = self.prepare(
            "Lena,Fictional,lena@example.test,,,,,,\n=cmd,Fictional,bad-email,,,,,,\n"
        )
        before = self.client.get(reverse("import_skipped", args=[item.pk]))
        self.confirm(item)
        after = self.client.get(reverse("import_skipped", args=[item.pk]))
        for answer in (before, after):
            text = answer.content.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            self.assertEqual(rows[0][:3], ["Row", "Reason", "First Name"])
            self.assertEqual(
                rows[1][:5],
                ["3", "Email: Enter a valid email address.", "'=cmd", "Fictional", "bad-email"],
            )
            self.assertEqual(len(rows), 2)
        self.assertEqual(after["Cache-Control"], "private, no-store")
        self.assertEqual(AuditLog.objects.filter(action="data.exported").count(), 2)
        report = self.client.get(reverse("import_report", args=[item.pk]))
        self.assertContains(report, "Download the skipped rows")

        self.client.post(reverse("import_discard", args=[item.pk]))
        item.refresh_from_db()
        self.assertIsNone(item.content)
        self.assertEqual(item.skipped[0]["row"], 3)  # The report outlives the file.
        report = self.client.get(reverse("import_report", args=[item.pk]))
        self.assertContains(report, "The uploaded file has been deleted")
        gone = self.client.get(reverse("import_skipped", args=[item.pk]))
        self.assertRedirects(gone, reverse("import_list"))

    def test_cancelling_and_old_files(self):
        item = self.upload(HEADERS + "Lena,Fictional,lena@example.test,,,,,,\n")
        self.client.post(reverse("import_discard", args=[item.pk]))
        self.assertFalse(SpreadsheetImport.objects.exists())

        waiting = self.upload(HEADERS + "Lena,Fictional,lena@example.test,,,,,,\n")
        self.assertEqual(str(waiting), "clients.csv")
        self.assertRedirects(
            self.client.get(reverse("import_report", args=[waiting.pk])),
            reverse("import_columns", args=[waiting.pk]),
        )
        done = self.confirm(self.prepare("Omar,Fictional,omar@example.test,,,,,,\n"))
        fresh = self.upload(HEADERS + "Nadia,Fictional,nadia@example.test,,,,,,\n")
        old = timezone.now() - timedelta(days=2)
        SpreadsheetImport.objects.filter(pk__in=[waiting.pk, done.pk]).update(created_at=old)
        self.assertEqual(tick("test-worker"), 0)  # The worker deletes files a day old.
        self.assertFalse(SpreadsheetImport.objects.filter(pk=waiting.pk).exists())
        done.refresh_from_db()
        self.assertIsNone(done.content)
        self.assertEqual(done.contacts_created, 1)
        fresh.refresh_from_db()
        self.assertIsNotNone(fresh.content)

        SpreadsheetImport.objects.filter(pk=fresh.pk).update(content=None)
        for name in ("import_columns", "import_preview"):
            answer = self.client.get(reverse(name, args=[fresh.pk]))
            self.assertRedirects(answer, reverse("import_list"))
        self.assertEqual(spreadsheet.run_import(fresh, None).status, "preparing")
        self.assertEqual(Contact.objects.filter(first_name="Nadia").count(), 0)

    def test_template_and_refused_upload(self):
        CustomField.objects.create(
            applies_to="matter", label="Biometrics", key="biometrics", field_type="date"
        )
        CustomField.objects.create(
            applies_to="matter", label="Pro bono", key="pro_bono", field_type="yes_no"
        )
        guide = self.client.get(reverse("import_list"))
        self.assertContains(guide, "<td>Pro bono</td><td>Matter</td><td>Yes or no.</td>", html=True)
        self.assertContains(guide, "Prospective client")
        answer = self.client.get(reverse("import_template"))
        text = answer.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿First name,Last name,Email"))
        self.assertIn("Biometrics", text)
        page = self.client.post(
            reverse("import_list"), {"file": SimpleUploadedFile("book.xlsx", b"PK\x03\x04\x00")}
        )
        self.assertContains(page, "This is not a CSV file.")
        self.assertFalse(SpreadsheetImport.objects.exists())

    def test_only_people_who_may_import_import(self):
        self.client.force_login(make_user("paralegal", superuser=False))
        for name in ("import_list", "import_template"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.client.force_login(make_user("office", role="Office administrator"))
        self.assertEqual(self.client.get(reverse("import_list")).status_code, 200)
