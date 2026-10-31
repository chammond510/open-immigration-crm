from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.billing.models import Invoice
from crm.comms.models import Communication
from crm.models import Contact
from crm.notices.models import CaseNotice
from crm.search import CONTACT_SEARCH_FIELDS, words_match

from .factories import make_contact, make_matter, make_user


class WordsMatchTests(TestCase):
    def test_every_word_must_match_some_field(self):
        ana = make_contact(first_name="Ana", last_name="Muestra")
        make_contact(first_name="Ana", last_name="Ejemplo", email="ana.e@example.test")
        found = Contact.objects.filter(words_match("ana  MUESTRA", CONTACT_SEARCH_FIELDS))
        self.assertEqual(list(found), [ana])
        everyone = Contact.objects.filter(words_match("", CONTACT_SEARCH_FIELDS))
        self.assertEqual(everyone.count(), 2)


class PageSearchTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user())
        self.ana = make_contact(first_name="Ana", last_name="Muestra")
        self.other = make_contact(first_name="Ana", last_name="Ejemplo", email="e@example.test")
        self.matter = make_matter(self.ana)
        other_matter = make_matter(self.other)
        for contact, matter in ((self.ana, self.matter), (self.other, other_matter)):
            Invoice.objects.create(contact=contact, matter=matter)
            Communication.objects.create(channel="call", contact=contact, summary="Fictional")
            CaseNotice.objects.create(
                matter=matter, kind="receipt", notice_date=timezone.localdate()
            )

    def test_invoices_communications_and_notices_find_full_names(self):
        invoices = self.client.get(reverse("invoice_list"), {"q": "Ana Muestra"})
        self.assertEqual([i.contact for i in invoices.context["invoices"]], [self.ana])
        comms = self.client.get(reverse("communication_list"), {"q": "Ana Muestra"})
        self.assertEqual([c.contact for c in comms.context["communications"]], [self.ana])
        notices = self.client.get(reverse("notice_list"), {"q": "Ana Muestra"})
        self.assertEqual([n.matter for n in notices.context["notices"]], [self.matter])

    def test_matching_a_message_finds_full_names(self):
        unmatched = Communication.objects.create(channel="text", counterpart="+15550100199")
        page = self.client.get(
            reverse("communication_match", args=[unmatched.pk]), {"q": "Ana Muestra"}
        )
        self.assertEqual(list(page.context["candidates"]), [self.ana])
