import {Page, Route} from '@playwright/test';
import {test, expect} from '../../fixtures';
import {TEST_USERS} from '../../global-setup';
import {ApiClient} from '../../utils/api';
import {pushImage} from '../../utils/container';

const patchDigestInventory = async (route: Route, tagName: string) => {
  const response = await route.fetch();
  const body = await response.json();

  const patchTag = (tag: Record<string, unknown>) => {
    if (tag.name !== tagName && !tag.manifest_digest) return;
    const canonical = tag.manifest_digest as string;
    if (!canonical) return;
    const encoded = canonical.split(':')[1];
    const expand = (length: number) =>
      encoded.repeat(Math.ceil(length / encoded.length)).slice(0, length);
    tag.manifest_digests = [
      {
        digest: canonical,
        algorithm: 'sha256',
        is_enabled: false,
        is_preferred: false,
      },
      {
        digest: `sha384:${expand(96)}`,
        algorithm: 'sha384',
        is_enabled: true,
        is_preferred: true,
      },
      {
        digest: `sha512:${expand(128)}`,
        algorithm: 'sha512',
        is_enabled: true,
        is_preferred: false,
      },
    ];
  };

  if (Array.isArray(body.tags)) {
    body.tags.filter((tag) => tag.name === tagName).forEach(patchTag);
  } else if (body.tags?.[tagName]) {
    patchTag(body.tags[tagName]);
  }

  await route.fulfill({response, json: body});
};

const interceptRepositoryResponses = async (
  page: Page,
  namespace: string,
  repository: string,
  tagName: string,
) => {
  await page.route(
    `**/api/v1/repository/${namespace}/${repository}**`,
    (route) => patchDigestInventory(route, tagName),
  );
};

test.describe(
  'Repository manifest digest identities',
  {tag: ['@tags', '@repository', '@container', '@pqc']},
  () => {
    const tagName = 'digest-identities';
    let repository: {namespace: string; name: string; fullName: string};

    test.beforeAll(async ({userContext, cachedContainerAvailable}) => {
      if (!cachedContainerAvailable) return;
      const api = new ApiClient(userContext.request);
      const name = `digest-identities-${Date.now()}`;
      await api.createRepository(TEST_USERS.user.username, name, 'private');
      repository = {
        namespace: TEST_USERS.user.username,
        name,
        fullName: `${TEST_USERS.user.username}/${name}`,
      };
      await pushImage(
        repository.namespace,
        repository.name,
        tagName,
        TEST_USERS.user.username,
        TEST_USERS.user.password,
      );
    });

    test.afterAll(async ({userContext}) => {
      if (!repository) return;
      const api = new ApiClient(userContext.request);
      await api.deleteRepository(repository.namespace, repository.name);
    });

    test('React selects the preferred enabled identity and retains disabled identities', async ({
      authenticatedPage,
    }) => {
      await interceptRepositoryResponses(
        authenticatedPage,
        repository.namespace,
        repository.name,
        tagName,
      );
      await authenticatedPage.goto(
        `/repository/${repository.fullName}?tab=tags`,
      );

      const row = authenticatedPage.locator('tr').filter({
        has: authenticatedPage.getByRole('link', {name: tagName, exact: true}),
      });
      const selector = row.getByLabel('Manifest digest identity');
      await expect(selector).toHaveValue(/sha384:/);
      await expect(selector.locator('option')).toHaveCount(3);
      await expect(selector.locator('option').first()).toBeDisabled();

      const sha512Digest = await selector
        .locator('option')
        .filter({hasText: 'sha512:'})
        .getAttribute('value');
      expect(sha512Digest).not.toBeNull();

      await authenticatedPage.locator('#toolbar-dropdown-filter').click();
      await authenticatedPage.getByRole('menuitem', {name: 'Digest'}).click();
      await authenticatedPage
        .locator('#tagslist-search-input input')
        .fill(sha512Digest!);
      await expect(
        authenticatedPage.getByRole('link', {name: tagName, exact: true}),
      ).toBeVisible();
      await authenticatedPage.locator('#tagslist-search-input input').fill('');

      await selector.selectOption(sha512Digest!);
      const selectedDigest = await selector.inputValue();
      const tagHref = await row
        .getByRole('link', {name: tagName, exact: true})
        .getAttribute('href');
      expect(
        new URL(tagHref!, 'http://quay.test').searchParams.get('digest'),
      ).toBe(selectedDigest);
      await authenticatedPage
        .context()
        .grantPermissions(['clipboard-read', 'clipboard-write']);
      const digestCell = selector.locator('xpath=ancestor::td');
      await digestCell.hover();
      await digestCell.getByLabel('Copy manifest digest to clipboard').click();
      await expect
        .poll(() =>
          authenticatedPage.evaluate(() => navigator.clipboard.readText()),
        )
        .toBe(selectedDigest);
    });

    test('Angular selects the preferred enabled identity and retains disabled identities', async ({
      authenticatedPage,
    }) => {
      await authenticatedPage.goto('/angular', {waitUntil: 'networkidle'});
      await authenticatedPage.reload({waitUntil: 'networkidle'});
      await interceptRepositoryResponses(
        authenticatedPage,
        repository.namespace,
        repository.name,
        tagName,
      );
      await authenticatedPage.goto(
        `/repository/${repository.fullName}?tab=tags`,
      );

      const selector = authenticatedPage.getByLabel('Manifest digest identity');
      await expect(selector).toHaveValue(/sha384:/);
      await expect(selector.locator('option')).toHaveCount(3);
      await expect(selector.locator('option').first()).toBeDisabled();

      const sha512Digest = await selector
        .locator('option')
        .filter({hasText: 'sha512:'})
        .getAttribute('value');
      expect(sha512Digest).not.toBeNull();

      const filter = authenticatedPage.getByPlaceholder('Filter Tags...');
      await filter.fill(sha512Digest!);
      await expect(selector).toBeVisible();
      await filter.fill('');

      await selector.selectOption(sha512Digest!);
      const selectedDigest = await selector.inputValue();
      await expect(
        selector.locator('xpath=following-sibling::manifest-link').locator('a'),
      ).toHaveAttribute('href', new RegExp(selectedDigest));
    });
  },
);
