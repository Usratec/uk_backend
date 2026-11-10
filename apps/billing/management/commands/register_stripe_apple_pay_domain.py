from django.core.management.base import BaseCommand, CommandError

from apps.integrations import stripe_client


class Command(BaseCommand):
    help = "Register a domain for Apple Pay on the Stripe account these settings point at."

    def add_arguments(self, parser):
        parser.add_argument("domain", help="The domain serving the checkout page, e.g. stage.1theory.co.uk")

    def handle(self, *args, **options):
        domain = options["domain"].strip().lower()
        if not domain or "/" in domain:
            raise CommandError("Give a bare domain, with no scheme and no path.")

        try:
            registered = stripe_client.register_payment_method_domain(domain)
        except stripe_client.StripeError as error:
            raise CommandError(f"Stripe refused to register {domain}: {error}") from error

        self.stdout.write(f"Registered {domain} ({registered.id}).")
        for wallet, enabled in sorted(registered.wallets.items()):
            state = "ready" if enabled else "not ready — check the dashboard"
            self.stdout.write(f"  {wallet}: {state}")
