import {atom, atomFamily, selector} from 'recoil';
import {SearchState} from 'src/components/toolbar/SearchTypes';
import {Tag} from 'src/resources/TagResource';
import ColumnNames from 'src/routes/RepositoryDetails/Tags/ColumnNames';
import {manifestDigestIdentities} from 'src/libs/manifestDigests';

export const searchTagsState = atom<SearchState>({
  key: 'searchsearchTagsStateState',
  default: {
    query: '',
    field: ColumnNames.name,
    isRegEx: false,
  },
});

export const searchTagsFilterState = selector({
  key: 'searchFilter',
  get: ({get}) => {
    const search = get(searchTagsState);
    if (search.query === '') {
      return null;
    }

    const filterByName = (tag: Tag) => tag.name.includes(search.query);
    const filterByNameRegex = (tag: Tag) => {
      try {
        const regex = new RegExp(search.query, 'i');
        return regex.test(tag.name);
      } catch (e) {
        return false;
      }
    };
    const digestSearchValue = (tag: Tag) =>
      manifestDigestIdentities(tag.manifest_digests, tag.manifest_digest)
        .map((identity) => identity.digest)
        .join(' ');
    const filterByDigest = (tag: Tag) =>
      digestSearchValue(tag).includes(search.query);
    const filterByDigestRegex = (tag: Tag) => {
      try {
        const regex = new RegExp(search.query, 'i');
        return regex.test(digestSearchValue(tag));
      } catch (e) {
        return false;
      }
    };

    switch (search.field) {
      case ColumnNames.digest:
        if (search.isRegEx) {
          return filterByDigestRegex;
        } else {
          return filterByDigest;
        }
      case ColumnNames.name:
      default:
        if (search.isRegEx) {
          return filterByNameRegex;
        } else {
          return filterByName;
        }
    }
  },
});

export const paginationState = atom({
  key: 'paginationState',
  default: {
    page: 1,
    perPage: 25,
  },
});

export const selectedTagsState = atom({
  key: 'selectedTagsState',
  default: [],
});

export const currentOpenPopoverState = atom({
  key: 'currentOpenPopoverState',
  default: '',
});

export const childManifestSizeState = atomFamily({
  key: 'childManifestDigest',
  default: null,
});

export const showSignaturesState = atom({
  key: 'showSignaturesState',
  default: false,
});

export const expandedViewState = atom({
  key: 'expandedViewState',
  default: false,
});
