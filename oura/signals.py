"""Django signals emitted by the webhook receiver.

``notification_received`` fires for every verified POST to the receiver view.
Connect a handler to drive ingest:

.. code-block:: python

    from django.dispatch import receiver
    from oura.signals import notification_received
    from oura.webhooks import process_notification

    @receiver(notification_received)
    def on_notification(sender, payload, **kwargs):
        process_notification(payload)  # or hand off to celery / a queue

Sender is ``None`` (signal is namespace-only). The ``payload`` keyword carries
the parsed JSON body exactly as Oura sent it. Oura expects a 2xx within 10
seconds and retries failures ~10 times — if processing is heavier than a few
seconds, hand off to a queue rather than processing inline.
"""

import django.dispatch

notification_received = django.dispatch.Signal()
