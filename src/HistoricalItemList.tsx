import React, { FC, useMemo, useState, useEffect } from "react";
import Grid from "@mui/material/Grid";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Switch from "@mui/material/Switch";
import FormControlLabel from "@mui/material/FormControlLabel";
import BreadCrumbs from "./BreadCrumbs";
import HistoricalItemDetail from "./HistoricalItemDetail";
import { MenuItemType } from "./types";
import {
    graphql,
    useQueryLoader,
    usePreloadedQuery,
    PreloadedQuery,
} from "react-relay";
import { HistoricalItemListQuery } from "./__generated__/HistoricalItemListQuery.graphql";

const HistoricalItemsQuery = graphql`
    query HistoricalItemListQuery(
        $type: String
        $limit: Int!
        $offset: Int!
        $applyInvertedPermanentRules: Boolean!
    ) {
        historicalItems(
            filters: [{ type: $type }]
            limit: $limit
            offset: $offset
            applyInvertedPermanentRules: $applyInvertedPermanentRules
        ) {
            nodes {
                id
                type
                uid
                title
                checkedTitle
                posterUrl
                attributes {
                    key
                    values
                    details
                }
                added
            }
            pageInfo {
                hasNextPage
                hasPreviousPage
                startOffset
                endOffset
                totalCount
            }
        }
    }
`;

const PAGE_SIZE = 12;

const HistoricalItemListContent: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<HistoricalItemListQuery>;
    type: string;
    setType: (t: string) => void;
    offset: number;
    setOffset: (o: number) => void;
    applyInvertedPermanentRules: boolean;
    setApplyInvertedPermanentRules: (v: boolean) => void;
}> = ({
    menuItem,
    queryRef,
    type,
    setType,
    offset,
    setOffset,
    applyInvertedPermanentRules,
    setApplyInvertedPermanentRules,
}) => {
    const data = usePreloadedQuery<HistoricalItemListQuery>(
        HistoricalItemsQuery,
        queryRef
    );
    const items = data.historicalItems.nodes;
    const pageInfo = data.historicalItems.pageInfo;
    const crumbs = useMemo(() => [menuItem], [menuItem]);
    return (
        <Box sx={{ position: "relative" }}>
            <BreadCrumbs crumbs={crumbs} />
            <Box sx={{ mb: 2 }}>
                <Button
                    variant={type === "mv" ? "contained" : "outlined"}
                    onClick={() => {
                        setType("mv");
                        setOffset(0);
                    }}
                    sx={{ mr: 1 }}
                >
                    Movie History
                </Button>
                <Button
                    variant={type === "tv" ? "contained" : "outlined"}
                    onClick={() => {
                        setType("tv");
                        setOffset(0);
                    }}
                >
                    Show History
                </Button>
                <FormControlLabel
                    sx={{ ml: 1 }}
                    control={
                        <Switch
                            color="primary"
                            checked={applyInvertedPermanentRules}
                            onChange={(e) => {
                                setApplyInvertedPermanentRules(
                                    e.target.checked
                                );
                                setOffset(0);
                            }}
                        />
                    }
                    label="Apply Filter Rules"
                />
            </Box>
            <Grid container spacing={2}>
                {items.map((item: any) => (
                    <HistoricalItemDetail key={item.uid} item={item} />
                ))}
            </Grid>
            <Box sx={{ mt: 2, display: "flex", justifyContent: "center" }}>
                <Button
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                    disabled={!pageInfo.hasPreviousPage}
                >
                    Previous
                </Button>
                <Box sx={{ mx: 2, alignSelf: "center" }}>
                    {pageInfo.startOffset + 1} - {pageInfo.endOffset + 1} of{" "}
                    {pageInfo.totalCount}
                </Box>
                <Button
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                    disabled={!pageInfo.hasNextPage}
                >
                    Next
                </Button>
            </Box>
        </Box>
    );
};

const HistoricalItemList: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const [offset, setOffset] = useState(0);
    const [type, setType] = useState(menuItem.typeName || "mv");
    const [applyInvertedPermanentRules, setApplyInvertedPermanentRules] =
        useState(true);
    const [queryRef, loadQuery, disposeQuery] =
        useQueryLoader<HistoricalItemListQuery>(HistoricalItemsQuery);

    useEffect(() => {
        loadQuery({
            type,
            limit: PAGE_SIZE,
            offset,
            applyInvertedPermanentRules,
        });
        return () => {
            disposeQuery();
        };
    }, [type, offset, applyInvertedPermanentRules, loadQuery, disposeQuery]);

    return queryRef ? (
        <HistoricalItemListContent
            menuItem={menuItem}
            queryRef={queryRef}
            type={type}
            setType={setType}
            offset={offset}
            setOffset={setOffset}
            applyInvertedPermanentRules={applyInvertedPermanentRules}
            setApplyInvertedPermanentRules={setApplyInvertedPermanentRules}
        />
    ) : null;
};

export default HistoricalItemList;
