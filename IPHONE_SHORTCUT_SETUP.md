# iPhone Shortcut Setup

This is the easiest strong phone workflow for the Execution Engine.

## Goal

Put one button on your iPhone home screen that always opens the current task check-in page.

That way the loop becomes:

- notification arrives
- tap home-screen shortcut or notification link
- answer three pulse questions
- press one action
- get back to your work

## Shortcut: Open Current Task Check-in

Create a Shortcut named `Execution Engine`.

Add these actions:

1. `URL`
   - value: `https://your-domain.example/active/checkin`

2. `Open URLs`

That is enough.

Because `/active/checkin` always redirects to the current task, the shortcut does not need to know any task IDs.

## Minimum viable setup

If you want the least setup possible, do only this:

1. Host the app on Railway or Render.
2. Open `https://your-domain.example/login` in Safari and log in once.
3. Create one Shortcut with the `URL` and `Open URLs` actions.
4. Add the Shortcut to your Home Screen.

Then your phone workflow is:

- tap the Home Screen icon
- answer `Progress`, `Energy`, `Friction`
- tap `Continue`, `Switch`, `Break`, `Complete`, or `Blocked`
- return to work

## Home Screen bookmark alternative

If you do not want to use Shortcuts, you can also use Safari:

1. Open `{PUBLIC_BASE_URL}/login`
2. Log in
3. Open `{PUBLIC_BASE_URL}/active/checkin`
4. Tap Share
5. Tap `Add to Home Screen`

This is the absolute simplest phone entry path.

## Optional Shortcut: Show Current Task First

If you want a confirmation screen before opening the check-in page, create a second shortcut:

1. `URL`
   - value: `https://your-domain.example/active`
2. `Get Contents of URL`
   - method: `GET`
3. `Get Dictionary Value`
   - key: `task_name`
4. `Show Result`

You can then branch from the returned `checkin_url` if you want something fancier, but the redirect shortcut above is the simpler option.

## Best practice

Pin the `Execution Engine` shortcut to:

- Home Screen
- Lock Screen widget
- Action Button, if your phone supports it

That gives you an instant recovery path even if you dismiss a Telegram notification.

## Best daily use

- Use the shortcut or Home Screen bookmark whenever you need to re-enter the current task without thinking.
- Use `/active/checkin` as the canonical front door from your phone.
- Treat the phone interaction as a checkpoint, not a planning session.
