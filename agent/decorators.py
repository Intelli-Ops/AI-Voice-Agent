import asyncio
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden

from .models import Profile


def role_required(role):
    """Require the logged-in user's Profile.role to match `role`. Stacks on
    top of login_required, so an anonymous visitor is redirected to the
    login page first; a logged-in user with the wrong role gets a 403
    instead of being silently redirected anywhere.

    Handles both sync and async views -- needed since some of our views
    are `async def` (they await pipeline/Twilio calls) and others aren't.
    Async views use request.auser()/Profile.objects.afirst() rather than
    the plain attribute access the sync branch uses, since touching the
    ORM synchronously from an async view raises SynchronousOnlyOperation.
    """

    def decorator(view_func):
        if asyncio.iscoroutinefunction(view_func):

            @wraps(view_func)
            async def _wrapped(request, *args, **kwargs):
                user = await request.auser()
                profile = await Profile.objects.filter(user=user).afirst()
                if profile is None or profile.role != role:
                    return HttpResponseForbidden("You don't have access to this page.")
                return await view_func(request, *args, **kwargs)

        else:

            @wraps(view_func)
            def _wrapped(request, *args, **kwargs):
                profile = getattr(request.user, "profile", None)
                if profile is None or profile.role != role:
                    return HttpResponseForbidden("You don't have access to this page.")
                return view_func(request, *args, **kwargs)

        return login_required(_wrapped)

    return decorator
