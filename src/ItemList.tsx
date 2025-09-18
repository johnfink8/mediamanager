import React, { FC, useEffect, useMemo } from "react";
import Grid from "@mui/material/Grid";
import Box from "@mui/material/Box";

import ItemDetail from "./ItemDetail";
import { MenuItemType } from "./types";
import BreadCrumbs from "./BreadCrumbs";
import {
    graphql,
    PreloadedQuery,
    usePreloadedQuery,
    useQueryLoader,
} from "react-relay";
import { Filter, ItemListQuery } from "./__generated__/ItemListQuery.graphql";
import { Backdrop, Card, CardContent, Typography } from "@mui/material";
import { useFilterContext } from "./TempFilterContext";

const ItemListQueryGQL = graphql`
    query ItemListQuery($filters: [Filter!]) {
        items(filters: $filters) {
            id
            nodes {
                id
                type
                uid
                title
                added
                checkedTitle
                posterUrl
                attributes {
                    key
                    values
                    details
                }
            }
        }
    }
`;

export const AddItemMutation = graphql`
    mutation ItemListAddItemMutation($input: AddItemInput!) {
        addItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const DeleteItemMutation = graphql`
    mutation ItemListDeleteItemMutation($input: AddItemInput!) {
        deleteItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const SetItemAddedMutation = graphql`
    mutation ItemListSetItemAddedMutation($input: SetItemAddedInput!) {
        setItemAdded(data: $input) {
            id
            added
        }
    }
`;


const ItemList: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<ItemListQuery>;
    filters: Filter[];
}> = ({ menuItem, queryRef }) => {
    const queryItems = usePreloadedQuery(ItemListQueryGQL, queryRef).items.nodes;
    const items = queryItems.filter(Boolean);
    const { setAttributeKeys } = useFilterContext();
    React.useEffect(() => {
        // Collect all unique attribute keys from the items
        const keys = new Set<string>();
        items.forEach(item => {
            item.attributes?.forEach(attr => {
                if (attr.key) keys.add(attr.key);
            });
        });
        setAttributeKeys(Array.from(keys));
    }, [items, setAttributeKeys]);
    // No need to filter by type here; backend handles it
    return (
        <Box sx={{ position: "relative" }}>
            <BreadCrumbs crumbs={[menuItem]} />
            <Grid container spacing={2}>
                {items.map((item) => (
                    <ItemDetail key={item.uid} item={item} />
                ))}
            </Grid>
        </Box>
    );
};

const ItemListLoading: FC = () => (
    <Grid container spacing={2}>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
    </Grid>
);

const ItemListContainer: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const { tempFilters } = useFilterContext();
    const [queryRef, loadQuery, disposeQuery] = useQueryLoader<ItemListQuery>(ItemListQueryGQL);
    // Always include the type filter, plus all temp filters mapped to Filter type
    const filters = useMemo<Filter[]>(
        () => [
            { type: menuItem.typeName },
            ...tempFilters.map(f => ({
                attribute: f.attribute,
                operator: f.operator,
                value: f.value,
            }))
        ],
        [menuItem, tempFilters]
    );
    useEffect(() => {
        loadQuery({ filters });
        return () => {
            disposeQuery();
        };
    }, [filters, loadQuery, disposeQuery]);
    return queryRef ? (
        <ItemList menuItem={menuItem} queryRef={queryRef} filters={filters} />
    ) : (
        <ItemListLoading />
    );
};

export default React.memo(ItemListContainer);
