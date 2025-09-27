import React, { FC, useCallback, useEffect, useMemo, useState } from "react";
import { graphql, usePreloadedQuery, useQueryLoader, PreloadedQuery } from "react-relay";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import TextField from "@mui/material/TextField";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import CardMedia from "@mui/material/CardMedia";
import Typography from "@mui/material/Typography";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";

import BreadCrumbs from "./BreadCrumbs";
import Loader from "./Loader";
import { MenuItemType } from "./types";
import { MovieRecommendationQuery } from "./__generated__/MovieRecommendationQuery.graphql";

const RecommendationQuery = graphql`
    query MovieRecommendationQuery($prompt: String) {
        movieRecommendation(prompt: $prompt) {
            imdbId
            title
            overview
            posterUrl
            year
            genres
            cast
            reason
            source
            prompt
            excludedRecent
        }
    }
`;

const RecommendationContent: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<MovieRecommendationQuery>;
    onRefresh: () => void;
    isLoading: boolean;
}> = ({ menuItem, queryRef, onRefresh, isLoading }) => {
    const data = usePreloadedQuery<MovieRecommendationQuery>(RecommendationQuery, queryRef);
    const recommendation = data.movieRecommendation;
    const crumbs = useMemo(() => [menuItem], [menuItem]);

    const genres = recommendation?.genres ?? [];
    const cast = recommendation?.cast ?? [];

    return (
        <Box sx={{ position: "relative" }}>
            <BreadCrumbs crumbs={crumbs} />
            <Loader open={isLoading} />
            <Stack spacing={3}>
                {recommendation ? (
                    <Card sx={{ display: "flex", flexWrap: "wrap" }}>
                        {recommendation.posterUrl ? (
                            <CardMedia
                                component="img"
                                image={recommendation.posterUrl}
                                alt={`${recommendation.title} poster`}
                                sx={{ width: { xs: "100%", sm: 260 }, maxHeight: 390 }}
                            />
                        ) : null}
                        <CardContent sx={{ flex: 1, minWidth: 260 }}>
                            <Typography variant="h5" component="h2" gutterBottom>
                                {recommendation.title}
                                {recommendation.year ? ` (${recommendation.year})` : ""}
                            </Typography>
                            {recommendation.reason ? (
                                <Typography variant="subtitle1" color="text.secondary" gutterBottom>
                                    {recommendation.reason}
                                </Typography>
                            ) : null}
                            {recommendation.overview ? (
                                <Typography paragraph>{recommendation.overview}</Typography>
                            ) : (
                                <Typography paragraph color="text.secondary">
                                    We could not find a synopsis for this title.
                                </Typography>
                            )}
                            {genres.length ? (
                                <Box sx={{ mb: 2 }}>
                                    <Typography variant="subtitle2" color="text.secondary">
                                        Genres
                                    </Typography>
                                    <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap" }}>
                                        {genres.map((genre) => (
                                            <Chip key={genre} label={genre} color="primary" variant="outlined" />
                                        ))}
                                    </Stack>
                                </Box>
                            ) : null}
                            {cast.length ? (
                                <Box>
                                    <Typography variant="subtitle2" color="text.secondary">
                                        Cast
                                    </Typography>
                                    <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap" }}>
                                        {cast.map((person) => (
                                            <Chip key={person} label={person} variant="outlined" />
                                        ))}
                                    </Stack>
                                </Box>
                            ) : null}
                        </CardContent>
                    </Card>
                ) : (
                    <Paper elevation={0} sx={{ p: 4, textAlign: "center" }}>
                        <Typography variant="h6" gutterBottom>
                            We couldn&apos;t generate a recommendation right now.
                        </Typography>
                        <Typography variant="body2" color="text.secondary">
                            Try refining your prompt or refreshing for another suggestion.
                        </Typography>
                        <Button sx={{ mt: 2 }} variant="contained" onClick={onRefresh}>
                            Try Again
                        </Button>
                    </Paper>
                )}

                {recommendation && recommendation.excludedRecent.length ? (
                    <Paper variant="outlined" sx={{ p: 2 }}>
                        <Typography variant="subtitle2" gutterBottom>
                            Skipped recently played titles
                        </Typography>
                        <Typography variant="body2" color="text.secondary">
                            {recommendation.excludedRecent.join(", ")}
                        </Typography>
                    </Paper>
                ) : null}
            </Stack>
        </Box>
    );
};

const MovieRecommendation: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const [prompt, setPrompt] = useState<string>("");
    const [isLoading, setIsLoading] = useState(false);
    const [queryRef, loadQuery, disposeQuery] = useQueryLoader<MovieRecommendationQuery>(RecommendationQuery);

    useEffect(() => {
        setIsLoading(true);
        loadQuery({ prompt: null });
        return () => {
            disposeQuery();
        };
    }, [loadQuery, disposeQuery]);

    const requestRecommendation = useCallback(() => {
        setIsLoading(true);
        loadQuery(
            { prompt: prompt.trim() ? prompt.trim() : null },
            {
                fetchPolicy: "network-only",
            }
        );
    }, [loadQuery, prompt]);

    const handlePromptChange = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        setPrompt(event.target.value);
    }, []);

    useEffect(() => {
        if (!queryRef) {
            return;
        }
        setIsLoading(false);
    }, [queryRef]);

    const controls = (
        <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
            <Typography variant="h6" gutterBottom>
                Tell us what kind of movie you&apos;re in the mood for
            </Typography>
            <Stack spacing={2} direction={{ xs: "column", sm: "row" }}>
                <TextField
                    fullWidth
                    label="Optional prompt"
                    placeholder="e.g. A feel-good adventure with a strong lead"
                    value={prompt}
                    onChange={handlePromptChange}
                    multiline
                    minRows={1}
                />
                <Stack direction="row" spacing={1} alignItems="center">
                    <Button variant="contained" onClick={requestRecommendation} disabled={isLoading}>
                        Recommend something
                    </Button>
                    <Divider flexItem orientation="vertical" sx={{ display: { xs: "none", sm: "block" } }} />
                    <Button variant="text" onClick={requestRecommendation} disabled={isLoading}>
                        Refresh
                    </Button>
                </Stack>
            </Stack>
        </Paper>
    );

    return (
        <Box>
            {controls}
            {queryRef ? (
                <RecommendationContent
                    menuItem={menuItem}
                    queryRef={queryRef}
                    onRefresh={requestRecommendation}
                    isLoading={isLoading}
                />
            ) : (
                <Loader open={true} />
            )}
        </Box>
    );
};

export default MovieRecommendation;
