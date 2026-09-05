import {Alert, PageSection, Title} from '@patternfly/react-core';
import {useEffect, useState} from 'react';
import {useLocation, useSearchParams} from 'react-router-dom';
import {QuayBreadcrumb} from 'src/components/breadcrumb/Breadcrumb';
import ErrorBoundary from 'src/components/errors/ErrorBoundary';
import RequestError from 'src/components/errors/RequestError';
import {addDisplayError, isErrorString} from 'src/resources/ErrorHandling';
import {
  ManifestByDigestResponse,
  Tag,
  TagsResponse,
  getManifestByDigest,
  getTags,
} from 'src/resources/TagResource';
import {useQuayConfig} from 'src/hooks/UseQuayConfig';
import {preferredManifestDigest} from 'src/libs/manifestDigests';
import {
  parseOrgNameFromUrl,
  parseRepoNameFromUrl,
  parseTagNameFromUrl,
} from '../../libs/utils';
import TagArchSelect from './TagDetailsArchSelect';
import TagTabs from './TagDetailsTabs';

function getMissingArchitectures(tag: Tag): string[] {
  if (!tag.is_sparse || !tag.manifest_list?.manifests) {
    return [];
  }
  return tag.manifest_list.manifests
    .filter((m) => m.is_present === false)
    .map((m) => `${m.platform.os}/${m.platform.architecture}`);
}

export default function TagDetails() {
  const [searchParams] = useSearchParams();
  const [digest, setDigest] = useState<string>('');
  const [manifestReference, setManifestReference] = useState<string>('');
  const [manifestData, setManifestData] =
    useState<ManifestByDigestResponse>(null);
  const [err, setErr] = useState<string>();
  const quayConfig = useQuayConfig();
  const [tagDetails, setTagDetails] = useState<Tag>({
    name: '',
    is_manifest_list: false,
    last_modified: '',
    manifest_digest: '',
    reversion: false,
    size: 0,
    start_ts: 0,
    manifest_list: {
      schemaVersion: 0,
      mediaType: '',
      manifests: [],
    },
  });

  // TODO: refactor, need more checks when parsing path
  const location = useLocation();

  const org = parseOrgNameFromUrl(location.pathname);
  const repo = parseRepoNameFromUrl(location.pathname);
  const tag = parseTagNameFromUrl(location.pathname);

  useEffect(() => {
    (async () => {
      try {
        const resp: TagsResponse = await getTags(org, repo, 1, 100, tag);

        if (resp.tags.length === 0) {
          throw new Error('Could not find tag');
        }
        if (resp.tags.length > 1) {
          throw new Error(
            'Unexpected response from API: more than one tag returned',
          );
        }

        const tagResp: Tag = resp.tags[0];
        const includeModelcard = quayConfig?.features.UI_MODELCARD || false;
        const rootDigest = preferredManifestDigest(
          tagResp.manifest_digests,
          tagResp.manifest_digest,
        );
        if (!rootDigest) {
          throw new Error('No registered digest identities');
        }
        const rootManifestData = await getManifestByDigest(
          org,
          repo,
          rootDigest,
          includeModelcard,
        );

        if (tagResp.is_manifest_list) {
          const manifestList = JSON.parse(rootManifestData.manifest_data);
          if (tagResp.child_manifests_presence && manifestList.manifests) {
            manifestList.manifests = manifestList.manifests.map(
              (manifest: {digest: string}) => ({
                ...manifest,
                is_present:
                  tagResp.child_manifests_presence?.[manifest.digest] ?? true,
              }),
            );
          }
          tagResp.manifest_list = manifestList;
        }
        if (rootManifestData.modelcard) {
          tagResp.modelcard = rootManifestData.modelcard;
        }

        const requestedDigest = searchParams.get('digest');
        const isEnabledRootDigest =
          tagResp.manifest_digests === undefined
            ? requestedDigest === tagResp.manifest_digest
            : tagResp.manifest_digests.some(
                (identity) =>
                  identity.is_enabled && identity.digest === requestedDigest,
              );
        const requestedChild = tagResp.manifest_list?.manifests?.find(
          (manifest) => manifest.digest === requestedDigest,
        );
        if (requestedDigest && !isEnabledRootDigest && !requestedChild) {
          throw new Error(`Requested digest not found: ${requestedDigest}`);
        }

        let selectedReference = tagResp.manifest_digest || rootDigest;
        let selectedIdentity = requestedDigest || rootDigest;
        let selectedManifestData = rootManifestData;

        if (tagResp.is_manifest_list && !isEnabledRootDigest) {
          const firstPresent = tagResp.manifest_list?.manifests?.find(
            (manifest) => manifest.is_present !== false,
          );
          selectedReference =
            requestedChild?.digest ?? firstPresent?.digest ?? rootDigest;
          if (selectedReference !== rootDigest) {
            selectedManifestData = await getManifestByDigest(
              org,
              repo,
              selectedReference,
            );
            selectedIdentity =
              preferredManifestDigest(
                selectedManifestData.manifest_digests,
                selectedManifestData.digest,
              ) ?? '';
          }
        }

        setManifestReference(selectedReference);
        setDigest(selectedIdentity);
        setManifestData(selectedManifestData);
        setTagDetails(tagResp);
        setErr(undefined);
      } catch (error: unknown) {
        console.error(error);
        const errorObj =
          error instanceof Error ? error : new Error(String(error));
        setErr(addDisplayError('Unable to get details for tag', errorObj));
      }
    })();
  }, [org, repo, tag, searchParams, quayConfig?.features?.UI_MODELCARD]);

  const selectManifestReference = async (reference: string) => {
    try {
      const selectedManifestData = await getManifestByDigest(
        org,
        repo,
        reference,
      );
      setManifestReference(reference);
      setManifestData(selectedManifestData);
      setDigest(
        preferredManifestDigest(
          selectedManifestData.manifest_digests,
          selectedManifestData.digest,
        ) ?? '',
      );
      setErr(undefined);
    } catch (error: unknown) {
      const errorObj =
        error instanceof Error ? error : new Error(String(error));
      setErr(addDisplayError('Unable to get details for manifest', errorObj));
    }
  };

  return (
    <>
      <QuayBreadcrumb />
      <PageSection hasBodyWrapper={false}>
        <Title headingLevel="h1">
          {repo}:{tag}
        </Title>
        <TagArchSelect
          digest={manifestReference}
          options={tagDetails.manifest_list?.manifests}
          setDigest={selectManifestReference}
          render={tagDetails.is_manifest_list}
          style={{marginTop: 'var(--pf-t--global--spacer--md)'}}
        />
        {tagDetails.is_sparse && (
          <Alert
            variant="warning"
            isInline
            title="Sparse Manifest List"
            style={{marginTop: 'var(--pf-t--global--spacer--md)'}}
            data-testid="sparse-manifest-alert"
          >
            This is a sparse manifest list - not all architectures are present
            locally.
            {getMissingArchitectures(tagDetails).length > 0 && (
              <>
                {' '}
                Missing architectures:{' '}
                {getMissingArchitectures(tagDetails).join(', ')}.
              </>
            )}{' '}
            Missing architectures will be pulled on first access.
          </Alert>
        )}
      </PageSection>
      <PageSection hasBodyWrapper={false} padding={{default: 'noPadding'}}>
        <ErrorBoundary
          hasError={isErrorString(err)}
          fallback={<RequestError message={err} />}
        >
          <TagTabs
            org={org}
            repo={repo}
            tag={tagDetails}
            digest={digest}
            manifestReference={manifestReference}
            manifestData={manifestData}
            setDigest={setDigest}
            err={err}
          />
        </ErrorBoundary>
      </PageSection>
    </>
  );
}
