# Nightways

Nightways has a build-free static frontend in `frontend/` and a FastAPI
backend in `backend/`.

FastAPI still serves the frontend at `/`, `/style.css`, and `/app.js` so the
existing Render site can remain online during the move to Cloudflare Pages.
The API remains available at `/api/nightways`.

## Frontend API configuration

`frontend/index.html` sets the backend URL with this metadata:

```html
<meta
    name="nightways-api-base-url"
    content="https://nightways.onrender.com"
>
```

The checked-in value lets the same files use Render from either Render itself
or Cloudflare Pages. An empty or missing value makes `frontend/app.js` fall
back to the original same-origin `/api/nightways` endpoint. The local launcher
also keeps same-origin behavior on `localhost`, `127.0.0.1`, and `[::1]`, so
local searches continue to use the local FastAPI process.

## Backend CORS configuration

Set `NIGHTWAYS_CORS_ORIGINS` on Render to a comma-separated list of exact
frontend origins. Root trailing slashes are normalized; URL paths, wildcards,
credentials, queries, and fragments are rejected when the application starts.

```text
NIGHTWAYS_CORS_ORIGINS=https://nightways.pages.dev,https://nightways.example
```

The default is an empty list, which keeps the current same-origin Render
frontend working without granting cross-origin access. Add preview deployment
origins individually if previews need to call the API; wildcard Pages origins
are intentionally not enabled.

Keep the existing Render build command, start command, and GISCO data setup.
They are managed outside this repository. The ASGI import remains
`backend.main:app`.

## Deploy the frontend to Cloudflare Pages

1. Connect this GitHub repository to a Pages project.
2. Select the production branch and use no framework preset.
3. Leave the root directory unset, use `exit 0` as the build command, and set
   the build output directory to `frontend`.
4. Before testing the Pages site, add its exact `https://<project>.pages.dev`
   origin to `NIGHTWAYS_CORS_ORIGINS` on the Render service and deploy the
   updated environment.
5. Save and deploy the Pages project. `frontend/index.html` is already at the
   root of the published directory.

Cloudflare documentation: [Git integration](https://developers.cloudflare.com/pages/get-started/git-integration/)
and [static HTML deployment](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/).

Render documentation: [environment variables](https://render.com/docs/configure-environment-variables)
and [deploys](https://render.com/docs/deploys).

## Tests

Run the backend suite from the repository root:

```powershell
python -B -m unittest discover -s backend -p "test_*.py" -v
```

`backend.test_origin_resolution.LiveOriginResolutionTests` makes live
Transitous requests and therefore requires outbound network access.
