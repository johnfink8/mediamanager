import React, { FC } from "react";
import CircularProgress from "@mui/material/CircularProgress";
import Backdrop from "@mui/material/Backdrop";

const Loader: FC<{ open: boolean }> = ({ open }) => (
    <Backdrop open={open} sx={{ position: "absolute" }}>
        <CircularProgress color="primary" />
    </Backdrop>
);
export default React.memo(Loader);
