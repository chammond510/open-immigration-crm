# Cal.com

Files consultations booked through Cal.com, on cal.com or on the firm's own self-hosted Cal.com. The contact is matched or created, the consult appointment is made, and reschedules and cancellations follow. It is in Beta: its tests run against a fake Cal.com that follows Cal.com's published API v2 and webhook documentation, but it has not run against a live account yet.

## What it does

- **Which bookings.** On the connection's page, the firm picks which of the account's Cal.com event types are consultations, from Cal.com's live list. Only bookings of those types are filed; others are ignored.
- **The contact.** A booking is matched to a contact by email or phone. If there is none, a contact is created at the consultation stage. A booker who gave only a phone number is matched by phone: Cal.com gives such bookers a stand-in `@sms.cal.com` address, which the CRM does not keep.
- **The appointment.** The consult appointment gets the event's title, time, and video link, and the answers to the firm's own booking questions. Answers to hidden questions are left out. It is assigned to the staff member hosting the booking when their CRM email matches.
- **Reschedules and cancellations.** Cal.com makes a new booking for a reschedule, naming the one it replaces, so the same appointment moves. A cancellation marks it cancelled.
- **Lead source.** Cal.com sends no tracking (UTM) values with bookings, so unlike Calendly this connection does not fill a lead-source field.

## Setting it up

1. In Cal.com, the account whose event types take consultations creates an API key under **Settings > Developer > API keys**.
2. In the CRM, someone who manages connections and AI (the Connections and AI permission, which Managing partners have by default) opens **Connections > Cal.com**, pastes the key, reviews what the connection sends and receives, and connects.
3. For a self-hosted Cal.com, set **API address** on the connection's page to the address of its API v2 service, such as `https://cal.example.test/api/v2`. Its API v2 service must be running. Plain HTTP is accepted only on this computer or the firm's own network.
4. On the connection's page, tick the event types that are consultations, and save.
5. Turn on webhooks with **Create the webhook in Cal.com** ticked. The CRM creates one webhook on the account for bookings, reschedules, and cancellations, signed with a new secret. Issuing a new address later replaces it, and disconnecting deletes it.
6. For team event types, add the same address and secret in Cal.com as a webhook on each team event type, for **Booking created**, **Booking rescheduled**, and **Booking cancelled**.

The webhook address must be reachable from Cal.com. Cal.com does not send a delivery again when the CRM did not accept it, so a booking made while the address was unreachable has to be added by hand.

## How deliveries are checked

Each delivery carries an `X-Cal-Signature-256` header: an HMAC-SHA256 of the body, made with the webhook's secret. The CRM refuses a delivery with a wrong or missing signature. Cal.com signs no separate timestamp, so the CRM also refuses a delivery whose `createdAt` is more than an hour off, and files each booking event only once.

Cal.com's own **Ping test** button is accepted and files nothing.
