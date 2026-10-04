# The static shell on Cloudflare Pages. Data publishing is separate (mk/r2.mk).
# Deploy the shell before publishing data in a widened schema: an old cached
# shell rejects new manifests.

.PHONY: deploy-build deploy-pages deploy

# Everything vite emits except data/.
deploy-build: ## Build the shell into dist-deploy/
	npm run build -- --mode deploy
	rm -rf dist-deploy
	mkdir -p dist-deploy
	rsync -a --exclude 'data/' dist/ dist-deploy/

deploy-pages: ## Publish dist-deploy/ to Cloudflare Pages
	npx wrangler pages deploy --branch main dist-deploy

deploy: deploy-build deploy-pages ## Build and deploy the shell
