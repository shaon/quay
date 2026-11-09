/*
 * v2 registry tests for proxied organizations
 */

import {test, expect, uniqueName} from '../../fixtures';
import {TEST_USERS} from '../../global-setup';
import {ApiClient} from '../../utils/api';
import {getV2Token} from '../../utils/api/auth';
import {API_URL} from '../../utils/config';
import {proxyOptionFromEnv} from '../../utils/proxy';
import {
  DigestAlgorithm,
  digestOf,
  getBlob,
  getManifest,
  isAlgorithmAllowed,
  newTestImage,
  pushImageByDigest,
} from '../../utils/registry-digests';

// Standalone APIRequestContexts don't inherit `use.proxy`; set it explicitly so
// registry calls work when Quay is reachable only via a proxy. undefined locally.
const REQUEST_PROXY = proxyOptionFromEnv();

test.describe(
  'V2 registry tests for proxied organizations',
  {tag: ['@api', '@v2', '@container', '@auth:Database']},
  () => {
    // Tests share same state (orgName, repoName, manifestDigest) and must
    // run sequentially.
    test.describe.configure({mode: 'serial'});

    const USERNAME = TEST_USERS.user.username;
    const PASSWORD = TEST_USERS.user.password;

    // shared state across all tests
    let orgName: string;
    let repoName: string;

    // blob information
    let blobDigest: string;
    let blobSize: number;

    test.beforeAll(async ({userContext, cachedContainerAvailable}) => {
      // skip setup if registry tooling is unavailable
      if (!cachedContainerAvailable) return;

      const api = new ApiClient(userContext.request);
      orgName = uniqueName('proxycache');

      // create organization
      await api.createOrganization(orgName, `${orgName}@example.com`);

      // create proxy organization
      await api.createProxyCacheConfig(orgName, {
        upstream_registry: 'quay.io',
        expiration_s: 3600,
        insecure: false,
      });
    });

    // cleanup
    test.afterAll(async ({userContext, cachedContainerAvailable}) => {
      if (!cachedContainerAvailable || !orgName) return;
      const api = new ApiClient(userContext.request);
      try {
        await api.deleteOrganization(orgName);
      } catch {
        /* ignore all API errors here */
      }
    });

    test('Test pull through proxy cache', async ({playwright}) => {
      const request = await playwright.request.newContext({
        ignoreHTTPSErrors: true,
        ...(REQUEST_PROXY ? {proxy: REQUEST_PROXY} : {}),
      });
      repoName = 'prometheus/busybox';

      try {
        const scope = `repository:${orgName}/${repoName}:push,pull`;
        const v2Token = await getV2Token(
          request,
          API_URL,
          USERNAME,
          PASSWORD,
          scope,
        );

        const tag = 'latest';

        // construct headers
        const headers = {
          authorization: `Bearer ${v2Token}`,
          accept: [
            `application/vnd.docker.distribution.manifest.v2+json`,
            `application/vnd.docker.distribution.manifest.list.v2+json`,
          ].join(', '),
        };

        // issue a GET manifest request
        const manifestPath = `${API_URL}/v2/${orgName}/${repoName}/manifests`;
        const blobPath = `${API_URL}/v2/${orgName}/${repoName}/blobs`;
        const r = await request.get(`${manifestPath}/${tag}`, {headers});

        expect(r.status()).toBe(200);
        const body = await r.json();

        // check if returned image is a manifest list
        if (body.manifests) {
          // got a manifest list so fetch platform specific digest
          const platformDigest = body.manifests[0].digest;
          const platformR = await request.get(
            `${manifestPath}/${platformDigest}`,
            {headers},
          );
          const platformBody = await platformR.json();
          blobDigest = platformBody.layers[0].digest;
          blobSize = platformBody.layers[0].size;
        } else {
          blobDigest = body.layers[0].digest;
          blobSize = body.layers[0].size;
        }

        // issue a blob fetch request (follows redirects) and verify the full blob is returned
        const blobRequest = await request.get(`${blobPath}/${blobDigest}`, {
          headers,
        });
        expect(blobRequest.status()).toBe(200);

        // verify that the amount of data provided is the same as the blob size
        const blobData = await blobRequest.body();
        expect(blobData.length).toBe(blobSize);
      } finally {
        await request.dispose();
      }
    });

    // Known limitation: it's currently difficult to verify that the 2nd pull is not coming from upstream
    // as blobs are shared across multiple tests and may already be saved in the backend (storage is content
    // addressable and there's only one blob and one ImageStorage entry per blob). Therefore, we just verify
    // that the blob can be pulled properly the 2nd time, with no errors raised.
    //
    // An improvement would be to add a full lifecycle:
    // 1. clean up the entire registry so that no blobs are stored locally
    // 2. create a completely new organization and proxy configuration
    // 3. pull the image from upstream to verify it contains _upstream_proxy in the Location header
    // 4. verify blob content (blob SHA and size)
    // 5. repeat the process verifying that there is no _upstream_proxy in the Location header
    // 6. verify blob content (blob SHA and size)
    test('second pull returns 200 with the correct content', async ({
      playwright,
      userContext,
    }) => {
      const request = await playwright.request.newContext({
        ignoreHTTPSErrors: true,
        ...(REQUEST_PROXY ? {proxy: REQUEST_PROXY} : {}),
      });

      try {
        const scope = `repository:${orgName}/${repoName}:push,pull`;
        const v2Token = await getV2Token(
          request,
          API_URL,
          USERNAME,
          PASSWORD,
          scope,
        );

        // construct headers
        const headers = {
          authorization: `Bearer ${v2Token}`,
        };

        // blob should be served from local storage
        // first, verify that the tag we previously pulled exists
        const tagResponse = await userContext.request.get(
          `${API_URL}/api/v1/repository/${orgName}/${repoName}/tag/?specificTag=latest&onlyActiveTags=true`,
        );

        expect(tagResponse.status()).toBe(200);
        const tagBody = await tagResponse.json();
        expect(tagBody.tags.length).toBeGreaterThan(0);

        // pull blob immediately
        const blobPath = `${API_URL}/v2/${orgName}/${repoName}/blobs`;
        const blobRequest = await request.get(`${blobPath}/${blobDigest}`, {
          headers,
        });
        expect(blobRequest.status()).toBe(200);

        // verify that the amount of data provided is the same as the blob size
        const blobData = await blobRequest.body();
        expect(blobData.length).toBe(blobSize);

        // Verify content-length header when present. Some environments (e.g. Prow
        // aws-s3 with OpenShift routing) omit this header on redirected S3 blob
        // responses; content delivery correctness is already verified above via
        // blobData.length. See: https://github.com/quay/quay/issues/7608
        const blobHeaderResponse = await blobRequest.headers();
        if ('content-length' in blobHeaderResponse) {
          expect(parseInt(blobHeaderResponse['content-length'])).toBe(blobSize);
        }
      } finally {
        await request.dispose();
      }
    });
  },
);

test.describe(
  'V2 proxy cache with SHA-384 and SHA-512 content',
  {tag: ['@api', '@v2', '@PROJQUAY-10117', '@auth:Database']},
  () => {
    // The local Quay is its own upstream: no public registry serves SHA-512 images. The proxy
    // organization's upstream is localhost:8080/<source org>, which needs `localhost` in
    // SSRF_ALLOWED_HOSTS (set in local-dev/stack/config.yaml).
    test.describe.configure({mode: 'serial'});

    const USERNAME = TEST_USERS.user.username;
    const PASSWORD = TEST_USERS.user.password;
    const UPSTREAM_HOST =
      process.env.PLAYWRIGHT_PQC_PROXY_UPSTREAM || 'localhost:8080';

    let sourceOrg: string;
    let proxyOrg: string;
    let proxyConfigError: string | null = null;

    test.beforeAll(async ({userContext}) => {
      const api = new ApiClient(userContext.request);
      sourceOrg = uniqueName('digestsrc');
      proxyOrg = uniqueName('digestproxy');
      await api.createOrganization(sourceOrg, `${sourceOrg}@example.com`);
      await api.createOrganization(proxyOrg, `${proxyOrg}@example.com`);
      try {
        await api.createProxyCacheConfig(proxyOrg, {
          upstream_registry: `${UPSTREAM_HOST}/${sourceOrg}`,
          expiration_s: 3600,
          insecure: true,
        });
      } catch (e) {
        proxyConfigError = String(e);
      }
    });

    test.afterAll(async ({userContext}) => {
      const api = new ApiClient(userContext.request);
      for (const org of [proxyOrg, sourceOrg]) {
        if (!org) continue;
        try {
          await api.deleteOrganization(org);
        } catch {
          /* ignore cleanup errors */
        }
      }
    });

    for (const algorithm of ['sha384', 'sha512'] as DigestAlgorithm[]) {
      test(`pull ${algorithm} content through the proxy cache`, async ({
        request,
        userContext,
        quayConfig,
      }) => {
        test.skip(
          !isAlgorithmAllowed(quayConfig, algorithm),
          `${algorithm} is not listed in ALLOWED_HASH_ALGORITHMS`,
        );
        test.skip(
          proxyConfigError !== null,
          `proxy cache upstream ${UPSTREAM_HOST} rejected (add it to SSRF_ALLOWED_HOSTS): ${proxyConfigError}`,
        );

        // Publish the SHA-384/512 image in a public source repository.
        const repoName = uniqueName(`img${algorithm}`);
        await new ApiClient(userContext.request).createRepository(
          sourceOrg,
          repoName,
          'public',
        );
        const sourceToken = await getV2Token(
          request,
          API_URL,
          USERNAME,
          PASSWORD,
          `repository:${sourceOrg}/${repoName}:push,pull`,
        );
        const image = newTestImage(`proxy-${algorithm}`);
        const pushed = await pushImageByDigest(
          request,
          `${sourceOrg}/${repoName}`,
          sourceToken,
          algorithm,
          image,
          ['latest'],
        );

        // Pull it through the proxy organization.
        const proxyRepo = `${proxyOrg}/${repoName}`;
        const proxyToken = await getV2Token(
          request,
          API_URL,
          USERNAME,
          PASSWORD,
          `repository:${proxyRepo}:push,pull`,
        );
        const manifest = await getManifest(
          request,
          proxyRepo,
          proxyToken,
          pushed.manifestDigest,
        );
        expect(manifest.status()).toBe(200);
        expect(manifest.headers()['docker-content-digest']).toBe(
          pushed.manifestDigest,
        );
        expect(Buffer.compare(await manifest.body(), pushed.manifest)).toBe(0);

        // HEAD returns the recorded size, GET returns the bytes under the requested digest.
        const head = await getBlob(
          request,
          proxyRepo,
          proxyToken,
          pushed.layerDigest,
          'HEAD',
        );
        expect(head.status()).toBe(200);
        expect(parseInt(head.headers()['content-length'])).toBe(
          image.layer.length,
        );

        const blob = await getBlob(
          request,
          proxyRepo,
          proxyToken,
          pushed.layerDigest,
        );
        expect(blob.status()).toBe(200);
        expect(Buffer.compare(await blob.body(), image.layer)).toBe(0);

        // Never served under the internal SHA-256 in the proxy repository.
        const internal = await getBlob(
          request,
          proxyRepo,
          proxyToken,
          digestOf('sha256', image.layer),
          'HEAD',
        );
        expect(internal.status()).toBe(404);
      });
    }

    test('disallowed digests are not proxied', async ({
      request,
      quayConfig,
    }) => {
      const disallowed = (['sha384', 'sha512'] as DigestAlgorithm[]).filter(
        (a) => !isAlgorithmAllowed(quayConfig, a),
      );
      test.skip(
        disallowed.length === 0,
        'every digest algorithm is allowed; rejection case does not apply',
      );
      const repo = `${proxyOrg}/${uniqueName('denied')}`;
      const token = await getV2Token(
        request,
        API_URL,
        USERNAME,
        PASSWORD,
        `repository:${repo}:pull`,
      );
      for (const algorithm of disallowed) {
        const r = await getManifest(
          request,
          repo,
          token,
          digestOf(algorithm, Buffer.from('anything')),
        );
        expect(r.status()).toBe(404);
      }
    });
  },
);
